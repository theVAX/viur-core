import json
from unittest import mock

from abstract import ViURTestCase


class TestCloudfunctionThumbnailerUnreadable(ViURTestCase):
    """A source the function cannot read (HTTP 422) is no outage: on request the caller learns about it."""

    def setUp(self):
        super().setUp()
        from viur.core import conf
        conf.file_hmac_key = b"app-key"
        conf.file_thumbnailer_url = "https://thumbnailer.example/"

    def _call(self, status, body, **kwargs):
        """`cloudfunction_thumbnailer` against a stub function that answers every call with *status* and *body*."""
        with mock.patch("google.cloud.storage.Client"):
            from viur.core.modules import file as file_module
        from viur.core import conf, current

        request = mock.Mock()
        request.request.host_url = "https://app.example/"
        current.request.set(request)

        def post(url, data, headers, **kw):
            return mock.Mock(status_code=status, json=lambda: body, text=json.dumps(body), content=b"")

        blob = mock.Mock(generate_signed_url=mock.Mock(return_value="https://storage.example/signed"))
        file = mock.Mock(get_bucket=lambda dlkey: mock.Mock(get_blob=lambda path: blob),
                         hmac_sign=file_module.File.hmac_sign)
        with mock.patch.object(conf, "main_app", mock.Mock(file=file)), \
                mock.patch.object(type(conf.instance), "is_dev_server", False), \
                mock.patch("google.auth.compute_engine.IDTokenCredentials"), \
                mock.patch.object(file_module.requests, "post", post):
            return file_module.cloudfunction_thumbnailer(
                {"dlkey": "key", "name": "image.png", "mimetype": "image/png"}, {}, [{"width": 480}], **kwargs)

    def test_unreadable_raises_on_request(self):
        from viur.core import errors

        unreadable = {"error": "Unreadable: cannot identify image file", "unreadable": True}
        with self.assertRaises(errors.UnprocessableEntity):
            self._call(422, unreadable, report_unreadable=True)

    def test_unreadable_is_logged_by_default(self):
        unreadable = {"error": "Unreadable: cannot identify image file", "unreadable": True}
        with self.assertLogs(level="ERROR"):
            self.assertIsNone(self._call(422, unreadable))

    def test_other_failures_stay_none(self):
        for status, body in ((500, {}), (503, {}), (200, {"error": "blocked url"})):
            with self.subTest(status=status), self.assertLogs(level="ERROR"):
                self.assertIsNone(self._call(status, body, report_unreadable=True))
