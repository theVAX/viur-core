import json
from unittest import mock

from abstract import ViURTestCase


class TestCloudfunctionThumbnailerTimeout(ViURTestCase):
    """The call of the function is bounded by `conf.file_thumbnailer_timeout`; a timeout counts as an outage."""

    def setUp(self):
        super().setUp()
        from viur.core import conf
        conf.file_hmac_key = b"app-key"
        conf.file_thumbnailer_url = "https://thumbnailer.example/"

    def _call(self, post):
        """`cloudfunction_thumbnailer` with *post* in place of `requests.post`."""
        with mock.patch("google.cloud.storage.Client"):
            from viur.core.modules import file as file_module
        from viur.core import conf, current

        request = mock.Mock()
        request.request.host_url = "https://app.example/"
        current.request.set(request)

        blob = mock.Mock(generate_signed_url=mock.Mock(return_value="https://storage.example/signed"))
        file = mock.Mock(get_bucket=lambda dlkey: mock.Mock(get_blob=lambda path: blob),
                         hmac_sign=file_module.File.hmac_sign)
        with mock.patch.object(conf, "main_app", mock.Mock(file=file)), \
                mock.patch.object(type(conf.instance), "is_dev_server", False), \
                mock.patch("google.auth.compute_engine.IDTokenCredentials"), \
                mock.patch.object(file_module.requests, "post", post):
            return file_module.cloudfunction_thumbnailer(
                {"dlkey": "key", "name": "image.png", "mimetype": "image/png"}, {}, [{"width": 480}])

    def test_timeout_is_passed_to_every_call(self):
        from viur.core import conf

        timeouts = []

        def post(url, data, headers, **kw):
            timeouts.append(kw.get("timeout"))
            return mock.Mock(status_code=200, json=lambda: {"values": []}, text=json.dumps({"values": []}))

        self.assertEqual(conf.file_thumbnailer_timeout, 30)
        with mock.patch.object(conf, "file_thumbnailer_timeout", 7.5):
            self._call(post)
        self.assertEqual(timeouts, [7.5, 7.5])  # `nameOnly` and the upload call

    def test_timeout_is_logged_and_gives_none(self):
        import requests

        def post(url, data, headers, **kw):
            raise requests.exceptions.ReadTimeout("read timed out")

        with self.assertLogs(level="ERROR") as logs:
            self.assertIsNone(self._call(post))
        self.assertIn("timed out", logs.output[0])
