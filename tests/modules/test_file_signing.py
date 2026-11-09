"""
Signing download URLs without a service account key (IAM ``signBlob``).

The signing credentials are built once per thread and reused; every URL is still signed on its own.
"""
import base64
import datetime
import json
import threading
from unittest import mock

import requests

from abstract import ViURTestCase

SIGNATURE = b"test-signature"


def _file_module():
    """Import the module with its signing credentials forgotten, as a fresh thread would see them."""
    # Lazy import: viur.core.modules.file initializes a GCS client at module level.
    with mock.patch("google.cloud.storage.Client"):
        from viur.core.modules import file
    _forget_signing_credentials(file)
    return file


def _forget_signing_credentials(file):
    if thread_local := getattr(file, "_signing_credentials", None):
        thread_local.__dict__.clear()


class TestSigningCredentialsAtCallSites(ViURTestCase):
    """``File.download`` and ``cloudfunction_thumbnailer`` reuse the signing credentials of their thread."""

    def setUp(self):
        super().setUp()
        from viur.core import conf
        self.file = _file_module()
        self.built = []
        id_token_credentials = mock.patch(
            "google.auth.compute_engine.IDTokenCredentials",
            side_effect=lambda *args, **kwargs: self.built.append(mock.Mock(name="credentials")) or self.built[-1],
        )
        for patcher in (id_token_credentials, mock.patch.object(conf.instance, "is_dev_server", False)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        _forget_signing_credentials(self.file)
        super().tearDown()

    def _download(self):
        """Run ``File.download`` up to the redirect to a signed URL; returns the credentials it signed with."""
        from viur.core import errors
        blob = mock.Mock(size=10 * 1024 * 1024)
        blob.name = "dlkey/source/a.pdf"
        blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed"
        module = mock.Mock()
        module.canDownload.return_value = True
        module.hmac_verify.return_value = True
        module.get_bucket.return_value.get_blob.return_value = blob
        blob_key = base64.urlsafe_b64encode(b"dlkey/source/a.pdf\x00209912312359").decode()
        with self.assertRaises(errors.Redirect):
            self.file.File.download._func(module, blob_key, sig="valid")
        return blob.generate_signed_url.call_args.kwargs["credentials"]

    def _thumbnailer(self):
        """Run ``cloudfunction_thumbnailer`` up to the call of the function; returns the credentials it signed with."""
        from viur.core import conf
        blob = mock.Mock()
        blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed"
        main_app = mock.Mock()
        main_app.file.get_bucket.return_value.get_blob.return_value = blob
        main_app.file.hmac_sign.return_value = "sig"
        request = mock.Mock()
        request.request.host_url = "https://app.example.com"
        self.file.current.request.set(request)
        with (
            mock.patch.object(conf, "main_app", main_app),
            mock.patch.object(conf, "file_thumbnailer_url", "https://thumbnailer.example.com"),
            mock.patch.object(self.file.requests, "post", return_value=mock.Mock(status_code=404)),
        ):
            self.file.cloudfunction_thumbnailer(
                {"dlkey": "dlkey", "name": "a.png", "mimetype": "image/png"}, {}, {})
        return blob.generate_signed_url.call_args.kwargs["credentials"]

    def test_download_builds_credentials_once(self):
        first, second = self._download(), self._download()
        self.assertEqual(1, len(self.built))
        self.assertIs(first, second)

    def test_thumbnailer_builds_credentials_once(self):
        first, second = self._thumbnailer(), self._thumbnailer()
        self.assertEqual(1, len(self.built))
        self.assertIs(first, second)

    def test_download_and_thumbnailer_share_credentials(self):
        self.assertIs(self._download(), self._thumbnailer())
        self.assertEqual(1, len(self.built))

    def test_each_thread_has_its_own_credentials(self):
        """google-auth refreshes the token without a lock, and ``requests.Session`` is not guaranteed to be
        thread-safe: a thread never uses the credentials of another one."""
        mine = [self._download(), self._download()]
        theirs = []
        worker = threading.Thread(target=lambda: theirs.extend([self._download(), self._download()]))
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(2, len(self.built))
        self.assertIs(mine[0], mine[1])
        self.assertIs(theirs[0], theirs[1])
        self.assertIsNot(mine[0], theirs[0])

    def test_failed_signature_drops_the_credentials(self):
        """A rejected token or a changed service account must not stick to the thread until the token expires."""
        from viur.core import errors
        module = mock.Mock()
        module.canDownload.return_value = True
        module.hmac_verify.return_value = True
        blob = module.get_bucket.return_value.get_blob.return_value
        blob.size, blob.name = 10 * 1024 * 1024, "dlkey/source/a.pdf"
        blob.generate_signed_url.side_effect = OSError("signBlob")
        blob_key = base64.urlsafe_b64encode(b"dlkey/source/a.pdf\x00209912312359").decode()
        with self.assertRaises(OSError):
            self.file.File.download._func(module, blob_key, sig="valid")
        blob.generate_signed_url.side_effect = None
        with self.assertRaises(errors.Redirect):
            self.file.File.download._func(module, blob_key, sig="valid")
        self.assertEqual(2, len(self.built))
        self.assertIs(self.built[1], blob.generate_signed_url.call_args.kwargs["credentials"])

    def test_failed_build_is_not_kept(self):
        with mock.patch("google.auth.compute_engine.IDTokenCredentials", side_effect=OSError("metadata")):
            with self.assertRaises(OSError):
                self._download()
        self._download()
        self.assertEqual(1, len(self.built))


class FakeGoogle:
    """Metadata server and IAM API; records every call as (method, path, authorization)."""

    def __init__(self):
        self.calls = []
        self.tokens = 0
        self.sign_status = 200

    @staticmethod
    def handles(url):
        return any(host in url for host in ("metadata.google.internal", "169.254.169.254", "googleapis.com"))

    def respond(self, method, url, headers):
        path = url.split("?")[0]
        self.calls.append((method, path, headers.get("authorization", "")))
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response.headers["Metadata-Flavor"] = "Google"
        if path.endswith("universe-domain"):
            response.headers["Content-Type"] = "text/plain"
            body = "googleapis.com"
        elif path.endswith("/token"):
            self.tokens += 1
            body = {"access_token": f"token-{self.tokens}", "expires_in": 3600, "token_type": "Bearer"}
        elif "service-accounts" in path:
            body = {"email": "app@test.iam.gserviceaccount.com", "scopes": ["cloud-platform"], "aliases": ["default"]}
        elif path.endswith(":signBlob"):
            response.status_code = self.sign_status
            body = {"keyId": "k", "signedBlob": base64.b64encode(SIGNATURE).decode()}
        elif path.endswith("/allowedLocations"):
            body = {"locations": ["europe-west3"], "encodedLocations": "0x1"}
        else:
            raise AssertionError(f"unexpected call {method} {url}")
        response._content = (body if isinstance(body, str) else json.dumps(body)).encode()
        return response

    def sign_calls(self):
        return [call for call in self.calls if call[1].endswith(":signBlob")]


class TestSigningCredentialsWithGoogleAuth(ViURTestCase):
    """The reused credentials against google-auth itself; only the HTTP transport is replaced."""

    def setUp(self):
        super().setUp()
        self.file = _file_module()
        self.google = FakeGoogle()
        original = requests.Session.request
        google = self.google

        def request(session, method, url, *args, **kwargs):
            if google.handles(url):
                headers = {key.lower(): value for key, value in (kwargs.get("headers") or {}).items()}
                return google.respond(method, url, headers)
            return original(session, method, url, *args, **kwargs)

        self.patcher = mock.patch.object(requests.Session, "request", request)
        self.patcher.start()
        self.addCleanup(self._stop_transport)

    def tearDown(self):
        _forget_signing_credentials(self.file)
        super().tearDown()

    @staticmethod
    def _join_background():
        """google-auth looks up the regional access boundary in a thread when that data needs refreshing; returns
        the threads still running after waiting for them."""
        threads = [thread for thread in threading.enumerate()
                   if type(thread).__name__ == "_RegionalAccessBoundaryRefreshThread"]
        for thread in threads:
            thread.join(5)
        return [thread for thread in threads if thread.is_alive()]

    def _stop_transport(self):
        # A thread still running keeps the replaced transport, so it cannot reach the network.
        if alive := self._join_background():
            self.fail(f"background threads still running: {alive}")
        self.patcher.stop()

    def _sign(self):
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import storage
        blob = storage.Client(project="test", credentials=AnonymousCredentials()).bucket("b").blob("dl/source/a.png")
        return self.file._iam_signed_url(blob, datetime.datetime.now() + datetime.timedelta(seconds=60), None)

    def test_later_signatures_only_call_sign_blob(self):
        self.assertIn(f"X-Goog-Signature={SIGNATURE.hex()}", self._sign())
        self._join_background()
        self.google.calls.clear()
        for _ in range(3):
            self.assertIn(f"X-Goog-Signature={SIGNATURE.hex()}", self._sign())
        self.assertEqual(
            [("POST", "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
                      "app@test.iam.gserviceaccount.com:signBlob")] * 3,
            [call[:2] for call in self.google.calls])

    def test_expired_token_is_renewed(self):
        self._sign()
        self.assertEqual("Bearer token-1", self.google.sign_calls()[-1][2])
        token_credentials = self.file._get_signing_credentials()._signer._credentials
        token_credentials.expiry -= datetime.timedelta(hours=2)
        self._sign()
        self.assertEqual(2, self.google.tokens)
        self.assertEqual("Bearer token-2", self.google.sign_calls()[-1][2])

    def test_sign_blob_error_is_raised(self):
        import google.auth.exceptions
        self.google.sign_status = 403
        with self.assertRaises(google.auth.exceptions.TransportError):
            self._sign()
        self.google.sign_status = 200
        self.assertIn(f"X-Goog-Signature={SIGNATURE.hex()}", self._sign())

    def test_rejected_token_is_not_reused(self):
        """``iam.Signer`` raises on 401 but keeps the token; the next signature must fetch a new one."""
        import google.auth.exceptions
        self._sign()
        self.google.sign_status = 401
        with self.assertRaises(google.auth.exceptions.TransportError):
            self._sign()
        self.google.sign_status = 200
        self._sign()
        self.assertEqual(2, self.google.tokens)
        self.assertEqual("Bearer token-2", self.google.sign_calls()[-1][2])
