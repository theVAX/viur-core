import json
import os
import subprocess
import sys
import textwrap

from abstract import ViURTestCase

# Imports viur.core.tasks and viur.core.modules.file in a fresh interpreter, because both do their GCP calls at
# import time. google.auth.default is mocked like in ViURTestCase.setUp; requests.get answers every metadata
# request with the status and body given on the command line.
SCRIPT = textwrap.dedent("""
    import json, sys
    from unittest import mock
    import google.auth
    google.auth.default = mock.Mock(return_value=(mock.Mock(), "test-project"))

    import requests

    def fake_get(url, *args, **kwargs):
        response = requests.Response()
        response.status_code = int(sys.argv[1])
        response._content = sys.argv[2].encode()
        response.url = url
        return response

    with mock.patch("requests.get", side_effect=fake_get) as get, mock.patch("google.cloud.storage.Client") as client:
        from viur.core import tasks
        from viur.core.modules import file

    region_calls = [c for c in get.call_args_list if c.args[0].endswith("/instance/region")]
    print(json.dumps({
        "region_call_kwargs": [sorted(c.kwargs) for c in region_calls],
        "queue_region": tasks.queueRegion,
        "lookup_bucket_calls": client.return_value.lookup_bucket.call_count,
        "bucket_calls": [list(c.args) for c in client.return_value.bucket.call_args_list],
        "private_bucket_is_bucket": file._private_bucket is client.return_value.bucket.return_value,
    }))
""")


class TestGcpCallsAtImport(ViURTestCase):

    def _import(self, status: int, body: str, gae_application: str) -> dict:
        env = {k: v for k, v in os.environ.items() if k != "GOOGLE_APPLICATION_CREDENTIALS"}
        env["GAE_APPLICATION"] = gae_application
        result = subprocess.run(
            [sys.executable, "-c", SCRIPT, str(status), body], capture_output=True, text=True, env=env,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout.splitlines()[-1])

    def test_region_lookup_has_a_timeout(self):
        res = self._import(200, "projects/123/regions/us-central1", "e~test-project")
        self.assertEqual(1, len(res["region_call_kwargs"]))
        self.assertIn("timeout", res["region_call_kwargs"][0])
        self.assertEqual("us-central1", res["queue_region"])

    def test_region_lookup_error_status_falls_back_to_the_prefix_table(self):
        res = self._import(500, "Internal Server Error", "h~test-project")
        self.assertEqual("europe-west3", res["queue_region"])

    def test_file_module_does_not_look_up_the_bucket_at_import(self):
        res = self._import(200, "projects/123/regions/europe-west3", "h~test-project")
        self.assertEqual(0, res["lookup_bucket_calls"])
        self.assertEqual([["test-project.appspot.com"]], res["bucket_calls"])
        self.assertTrue(res["private_bucket_is_bucket"])
