from unittest import mock

from abstract import ViURTestCase

FUNCTION_URL = "https://europe-west3-project.cloudfunctions.net/thumbnailer"


class TestCloudfunctionThumbnailerIdToken(ViURTestCase):
    """With `file_thumbnailer_auth = "id_token"` every call carries an ID token for the function url."""

    def setUp(self):
        super().setUp()
        from viur.core import conf
        conf.file_hmac_key = b"test-hmac-key-for-unit-tests"
        conf.file_thumbnailer_url = FUNCTION_URL

    def _calls(self, auth):
        """Call `cloudfunction_thumbnailer` against a stub function; return the headers of each call and the
        audiences ID tokens were fetched for."""
        with mock.patch("google.cloud.storage.Client"):
            from viur.core.modules import file as file_module
        from viur.core import conf, current

        request = mock.Mock()
        request.request.host_url = "https://app.example/"
        current.request.set(request)
        headers_sent, audiences = [], []

        def post(url, data, headers, **kwargs):
            headers_sent.append(headers)
            return mock.Mock(status_code=200, json=lambda: {"values": []})

        def fetch_id_token(auth_request, audience):
            audiences.append(audience)
            return "id-token"

        blob = mock.Mock(generate_signed_url=mock.Mock(return_value="https://storage.example/signed"))
        file = mock.Mock(get_bucket=lambda dlkey: mock.Mock(get_blob=lambda path: blob), hmac_sign=lambda data: "sig")
        with mock.patch.object(conf, "main_app", mock.Mock(file=file)), \
                mock.patch.object(type(conf.instance), "is_dev_server", False), \
                mock.patch.object(conf, "file_thumbnailer_auth", auth, create=True), \
                mock.patch("google.auth.compute_engine.IDTokenCredentials"), \
                mock.patch("google.oauth2.id_token.fetch_id_token", fetch_id_token), \
                mock.patch.object(file_module.requests, "post", post):
            file_module.cloudfunction_thumbnailer(
                {"dlkey": "key", "name": "image.png", "mimetype": "image/png"}, {}, [{"width": 480}])
        return headers_sent, audiences

    def test_id_token_for_the_function_url(self):
        headers, audiences = self._calls("id_token")
        self.assertEqual(["Bearer id-token"] * 2, [h.get("Authorization") for h in headers])
        self.assertEqual([FUNCTION_URL] * 2, audiences)

    def test_no_credentials_by_default(self):
        headers, audiences = self._calls(None)
        self.assertEqual([None] * 2, [h.get("Authorization") for h in headers])
        self.assertEqual([], audiences)
