import base64
import json
from unittest import mock

from abstract import ViURTestCase


class TestCloudfunctionThumbnailerBaseUrl(ViURTestCase):
    """The function trusts `baseUrl`; it must never come from the Host header outside the development server."""

    def setUp(self):
        super().setUp()
        from viur.core import conf
        conf.file_hmac_key = b"test-hmac-key-for-unit-tests"
        conf.file_thumbnailer_url = "https://thumbnailer.example/"

    def _first_payload(self, *, dev_server, base_url=None):
        """Call `cloudfunction_thumbnailer` against a stub function and return the data of the first call."""
        with mock.patch("google.cloud.storage.Client"):
            from viur.core.modules import file as file_module
        from viur.core import conf, current

        request = mock.Mock()
        request.request.host_url = "https://Attacker.example/"
        current.request.set(request)

        posted = []

        def post(url, data, headers, **kwargs):
            posted.append(json.loads(base64.b64decode(json.loads(data)["dataStr"])))
            return mock.Mock(status_code=200, json=lambda: {"values": []})

        blob = mock.Mock(generate_signed_url=mock.Mock(return_value="https://storage.example/signed"))
        file = mock.Mock(get_bucket=lambda dlkey: mock.Mock(get_blob=lambda path: blob),
                         create_download_url=lambda dlkey, name: "/file/download/signed",
                         hmac_sign=lambda data: "signature")
        with mock.patch.object(conf, "main_app", mock.Mock(file=file)), \
                mock.patch.object(type(conf.instance), "is_dev_server", dev_server), \
                mock.patch.object(conf, "file_thumbnailer_base_url", base_url, create=True), \
                mock.patch("google.auth.compute_engine.IDTokenCredentials"), \
                mock.patch.object(file_module.requests, "post", post):
            file_module.cloudfunction_thumbnailer(
                {"dlkey": "key", "name": "image.png", "mimetype": "image/png"}, {}, [{"width": 480}])
        return posted[0]

    def test_no_base_url_outside_the_development_server(self):
        data = self._first_payload(dev_server=False)
        self.assertEqual("", data["baseUrl"])
        self.assertEqual("https://storage.example/signed", data["url"])

    def test_configured_base_url_on_the_development_server(self):
        data = self._first_payload(dev_server=True, base_url="http://127.0.0.1:8080")
        self.assertEqual("http://127.0.0.1:8080", data["baseUrl"])
        self.assertEqual("/file/download/signed", data["url"])

    def test_request_host_on_the_development_server_without_setting(self):
        self.assertEqual("https://attacker.example/", self._first_payload(dev_server=True)["baseUrl"])
