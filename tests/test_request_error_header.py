import json
import types
from unittest import mock
from urllib.parse import unquote

import webob

from abstract import ViURTestCase

PRINTABLE_ASCII = {chr(c) for c in range(0x20, 0x7F)}


class TestErrorHeaderValue(ViURTestCase):
    """`x-viur-error` carries only printable ASCII, and the description can be restored from it."""

    def _value(self, descr) -> str:
        from viur.core.request import _error_header_value
        return _error_header_value(descr)

    def test_ascii_is_unchanged(self):
        descr = "Invalid value for 'name': must be <= 10 chars (see /docs?x=1&y=2)!"
        self.assertEqual(descr, self._value(descr))

    def test_non_latin1_is_encoded_and_restorable(self):
        descr = "File too large – “max” is 25 MB, naïve…"
        value = self._value(descr)
        self.assertEqual("File too large %E2%80%93 %E2%80%9Cmax%E2%80%9D is 25 MB, na%C3%AFve%E2%80%A6", value)
        self.assertEqual(descr, unquote(value))

    def test_only_printable_ascii(self):
        value = self._value("a\r\nX-Injected: 1\tb\x00c\x7fdé\U0001F600")
        self.assertLessEqual(set(value), PRINTABLE_ASCII)
        self.assertEqual("a%0DX-Injected: 1%09b%00c%7Fd%C3%A9%F0%9F%98%80", value)

    def test_newline_is_dropped_and_percent_is_escaped(self):
        self.assertEqual("line oneline two", self._value("line one\nline two"))
        self.assertEqual("100%25 sure", self._value("100% sure"))
        self.assertEqual("100% sure", unquote(self._value("100% sure")))

    def test_surrogate_and_non_str(self):
        self.assertEqual("bad ? here", self._value("bad \ud800 here"))
        self.assertEqual("b'raw'", self._value(b"raw"))

    def test_long_value_is_cut_between_characters(self):
        from viur.core.request import _ERROR_HEADER_MAX_LENGTH
        for descr in ("ß" * 1500, "x" + "ß" * 1500, "a" * 5000):
            with self.subTest(descr=descr[:2]):
                value = self._value(descr)
                self.assertLessEqual(len(value), _ERROR_HEADER_MAX_LENGTH)
                self.assertGreater(len(value), _ERROR_HEADER_MAX_LENGTH - 10)
                self.assertTrue(value.endswith("..."))
                restored = unquote(value[:-3], errors="strict")  # no cut inside a %XX or UTF-8 sequence
                self.assertTrue(descr.startswith(restored))
        exact = "a" * _ERROR_HEADER_MAX_LENGTH
        self.assertEqual(exact, self._value(exact))


class TestRouterErrorHeader(ViURTestCase):
    """A description that cannot be sent as is keeps status, body and `conf.error_handler` intact."""

    def setUp(self) -> None:
        super().setUp()
        from viur.core.config import conf

        for name in ("main_app", "main_resolver", "request_preprocessor", "error_handler"):
            self.addCleanup(setattr, conf, name, getattr(conf, name))
        conf.main_app = types.SimpleNamespace(
            json=types.SimpleNamespace(),
            vi=types.SimpleNamespace(),
            render=types.SimpleNamespace(getTemplateFileName=lambda *args, **kwargs: None),
        )
        conf.main_resolver = {}
        conf.error_handler = None

        session_patcher = mock.patch("viur.core.request.session.Session")
        session_patcher.start()
        self.addCleanup(session_patcher.stop)
        csp_patcher = mock.patch.object(conf.security, "content_security_policy", {})
        csp_patcher.start()
        self.addCleanup(csp_patcher.stop)

    @staticmethod
    def _request(descr) -> webob.Response:
        from viur.core import current, errors
        from viur.core.config import conf
        from viur.core.request import Router

        def preprocessor(path: str) -> str:
            current.request.get().response.headers["Content-Type"] = "application/json"
            raise errors.BadRequest(descr)

        conf.request_preprocessor = preprocessor
        return Router(webob.Request.blank("/json/x", base_url="https://localhost").environ).response

    def test_status_body_and_handler_survive(self):
        from viur.core.config import conf
        from viur.core.request import _ERROR_HEADER_MAX_LENGTH

        for descr in ("bad \ud800 here", b"raw bytes", "ß" * 1500):
            with self.subTest(descr=descr[:8]):
                conf.error_handler = mock.Mock(return_value="handled")
                response = self._request(descr)
                self.assertEqual(400, response.status_code)
                self.assertEqual(b"handled", response.body)
                conf.error_handler.assert_called_once()
                header = response.headers["x-viur-error"]
                self.assertLessEqual(set(header), PRINTABLE_ASCII)
                self.assertLessEqual(len(header), _ERROR_HEADER_MAX_LENGTH)

    def test_default_body_keeps_the_full_description(self):
        for descr in ("bad \ud800 here", "ß" * 1500):
            with self.subTest(descr=descr[:8]):
                response = self._request(descr)
                self.assertEqual(400, response.status_code)
                self.assertEqual(descr, json.loads(response.body)["descr"])
                self.assertLessEqual(set(response.headers["x-viur-error"]), PRINTABLE_ASCII)

    def test_header_failure_is_not_fatal(self):
        from viur.core.config import conf

        conf.error_handler = mock.Mock(return_value="handled")
        with mock.patch("viur.core.request._error_header_value", side_effect=RuntimeError("boom")):
            response = self._request("anything")
        self.assertEqual(400, response.status_code)
        self.assertEqual(b"handled", response.body)
        self.assertNotIn("x-viur-error", response.headers)
