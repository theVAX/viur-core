import base64
import json
import os
import subprocess
import sys
import textwrap
from unittest import mock

from abstract import ViURTestCase

# Imports viur.core.tasks and viur.core.modules.file in a fresh interpreter, because both do their GCP calls at
# import time. google.auth.default is mocked like in ViURTestCase.setUp; requests.get answers every metadata
# request with the status and body given on the command line. Status "timeout" sends the region request to a
# local socket that never answers, with the wait shortened to keep the test fast.
SCRIPT = textwrap.dedent("""
    import json, socket, sys
    from unittest import mock
    import google.auth
    google.auth.default = mock.Mock(return_value=(mock.Mock(), "test-project"))

    import requests

    real_get = requests.get
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()

    def fake_get(url, *args, **kwargs):
        if sys.argv[1] == "timeout" and url.endswith("/instance/region"):
            return real_get(f"http://127.0.0.1:{silent.getsockname()[1]}/", timeout=0.2)
        response = requests.Response()
        response.status_code = 404 if sys.argv[1] == "timeout" else int(sys.argv[1])
        response._content = sys.argv[2].encode()
        response.url = url
        return response

    with mock.patch("requests.get", side_effect=fake_get) as get, mock.patch("google.cloud.storage.Client") as client:
        from viur.core import tasks
        from viur.core.modules import file

    region_calls = [c for c in get.call_args_list if c.args[0].endswith("/instance/region")]
    print(json.dumps({
        "region_timeouts": [c.kwargs.get("timeout") for c in region_calls],
        "queue_region": tasks.queueRegion,
        "lookup_bucket_calls": client.return_value.lookup_bucket.call_count,
        "bucket_calls": [list(c.args) for c in client.return_value.bucket.call_args_list],
        "private_bucket_is_bucket": file._private_bucket is client.return_value.bucket.return_value,
    }))
""")


class TestGcpCallsAtImport(ViURTestCase):

    def _import(self, status: int | str, body: str, gae_application: str, tasks_region: str | None = None) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in ("GOOGLE_APPLICATION_CREDENTIALS", "VIUR_TASKS_REGION")}
        env["GAE_APPLICATION"] = gae_application
        if tasks_region:
            env["VIUR_TASKS_REGION"] = tasks_region
        result = subprocess.run(
            [sys.executable, "-c", SCRIPT, str(status), body], capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout.splitlines()[-1])

    def test_region_lookup_has_a_timeout(self):
        res = self._import(200, "projects/123/regions/us-central1", "e~test-project")
        self.assertEqual([3], res["region_timeouts"])
        self.assertEqual("us-central1", res["queue_region"])

    def test_region_lookup_error_status_falls_back_to_the_prefix_table(self):
        res = self._import(500, "Internal Server Error", "h~test-project")
        self.assertEqual("europe-west3", res["queue_region"])

    def test_region_lookup_timeout_falls_back_to_the_prefix_table(self):
        res = self._import("timeout", "", "h~test-project")
        self.assertEqual("europe-west3", res["queue_region"])

    def test_unknown_prefix_leaves_the_region_unresolved(self):
        for status in (500, "timeout"):
            with self.subTest(status=status):
                self.assertIsNone(self._import(status, "Internal Server Error", "x~test-project")["queue_region"])

    def test_explicit_region_is_the_fallback(self):
        for status in (500, "timeout"):
            with self.subTest(status=status):
                res = self._import(status, "Internal Server Error", "x~test-project", tasks_region="europe-west4")
                self.assertEqual("europe-west4", res["queue_region"])

    def test_file_module_does_not_look_up_the_bucket_at_import(self):
        res = self._import(200, "projects/123/regions/europe-west3", "h~test-project")
        self.assertEqual(0, res["lookup_bucket_calls"])
        self.assertEqual([["test-project.appspot.com"]], res["bucket_calls"])
        self.assertTrue(res["private_bucket_is_bucket"])


class MissingBucketHttp:
    """HTTP transport for google-cloud-storage that answers every request like a deleted bucket."""

    is_mtls = False

    def __init__(self):
        self.requests = []

    def request(self, method, url, *args, **kwargs):
        import requests
        self.requests.append((method, url))
        response = requests.Response()
        response.status_code = 404
        response.url = url
        response._content = json.dumps({"error": {"code": 404, "message": "The bucket does not exist."}}).encode()
        response.headers["Content-Type"] = "application/json"
        response.request = requests.Request(method, url).prepare()
        return response


class TestMissingPrivateBucket(ViURTestCase):
    """bucket() does not check that the bucket exists; these are the errors a missing private bucket causes."""

    def setUp(self):
        super().setUp()
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import storage
        with mock.patch("google.cloud.storage.Client"):
            from viur.core.modules import file
        self.file = file
        self.http = MissingBucketHttp()
        client = storage.Client(project="test-project", credentials=AnonymousCredentials(), _http=self.http)
        patch = mock.patch.object(file, "_private_bucket", client.bucket("test-project.appspot.com"))
        patch.start()
        self.addCleanup(patch.stop)

    def _module(self):
        module = mock.Mock()
        module.get_bucket = self.file.File.get_bucket
        module.is_valid_filename = self.file.File.is_valid_filename
        return module

    def test_download_answers_gone(self):
        from viur.core import errors
        module = self._module()
        module.canDownload.return_value = True
        module.hmac_verify.return_value = True
        blob_key = base64.urlsafe_b64encode(b"dlkey/source/a.txt\x000").decode()

        with self.assertRaises(errors.Gone):
            self.file.File.download._func(module, blob_key, sig="signature")
        self.assertTrue(self.http.requests, "the bucket was never asked")

    def test_upload_raises_not_found(self):
        from google.api_core.exceptions import NotFound

        with self.assertRaises(NotFound):
            self.file.File.write(self._module(), "a.txt", b"data")
