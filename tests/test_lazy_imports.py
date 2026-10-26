import json
import subprocess
import sys
import textwrap

from abstract import ViURTestCase

# Dependencies only a few code paths need; importing them at module level costs every instance start.
LAZY_MODULES = (
    "user_agents",
    "jsonschema",
    "google.cloud.bigquery",
    "qrcode",
    "google.cloud.recaptchaenterprise_v1",
    "PIL.ImageCms",
    "google.appengine.api.blobstore",
    "google.appengine.api.images",
    "google.appengine.api.memcache",
)

# Runs in a fresh interpreter, because this process has long imported everything.
# google.auth.default is mocked like in ViURTestCase.setUp; the App Engine testbed is left out on purpose,
# it imports the blobstore, images and memcache stubs itself.
SCRIPT = textwrap.dedent("""
    import json, sys
    from unittest import mock
    import google.auth
    google.auth.default = mock.Mock(return_value=(mock.Mock(), "test-project"))
    with mock.patch("google.cloud.storage.Client"):
        import viur.core
        import viur.core.bones
        import viur.core.modules.file
        import viur.core.modules.history
        import viur.core.modules.user
        import viur.core.render.html.env
    print(json.dumps(sorted(sys.modules)))
""")


class TestLazyImports(ViURTestCase):

    def test_rarely_used_dependencies_are_not_imported_at_startup(self):
        result = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        loaded = set(json.loads(result.stdout.splitlines()[-1]))
        self.assertEqual([], [name for name in LAZY_MODULES if name in loaded])
