import hashlib
import hmac
import json
from unittest import mock

from abstract import ViURTestCase


class TestCloudfunctionThumbnailerHmacKey(ViURTestCase):
    """The function must not need the key that signs the app's download urls."""

    def setUp(self):
        super().setUp()
        from viur.core import conf
        conf.file_hmac_key = b"app-key"
        conf.file_thumbnailer_url = "https://thumbnailer.example/"

    def _signed_calls(self, thumbnailer_key):
        """Call `cloudfunction_thumbnailer` against a stub function; return (dataStr, sign) of each call."""
        with mock.patch("google.cloud.storage.Client"):
            from viur.core.modules import file as file_module
        from viur.core import conf, current

        request = mock.Mock()
        request.request.host_url = "https://app.example/"
        current.request.set(request)
        posted = []

        def post(url, data, headers, **kwargs):
            body = json.loads(data)
            posted.append((body["dataStr"].encode("ASCII"), body["sign"]))
            return mock.Mock(status_code=200, json=lambda: {"values": []})

        blob = mock.Mock(generate_signed_url=mock.Mock(return_value="https://storage.example/signed"))
        file = mock.Mock(get_bucket=lambda dlkey: mock.Mock(get_blob=lambda path: blob),
                         hmac_sign=file_module.File.hmac_sign)
        with mock.patch.object(conf, "main_app", mock.Mock(file=file)), \
                mock.patch.object(type(conf.instance), "is_dev_server", False), \
                mock.patch.object(conf, "file_thumbnailer_hmac_key", thumbnailer_key, create=True), \
                mock.patch("google.auth.compute_engine.IDTokenCredentials"), \
                mock.patch.object(file_module.requests, "post", post):
            file_module.cloudfunction_thumbnailer(
                {"dlkey": "key", "name": "image.png", "mimetype": "image/png"}, {}, [{"width": 480}])
        return posted

    @staticmethod
    def _sign(key, data):
        return hmac.new(key, msg=data, digestmod=hashlib.sha3_384).hexdigest()

    def test_own_key_signs_the_calls(self):
        calls = self._signed_calls(b"thumbnailer-key")
        self.assertEqual(2, len(calls))
        for data, sign in calls:
            self.assertEqual(self._sign(b"thumbnailer-key", data), sign)
            self.assertNotEqual(self._sign(b"app-key", data), sign)

    def test_falls_back_to_the_file_key(self):
        for data, sign in self._signed_calls(None):
            self.assertEqual(self._sign(b"app-key", data), sign)
