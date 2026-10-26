import importlib
import json
import subprocess
import sys
import textwrap
from unittest import mock

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
)

# Runs in a fresh interpreter, because this process has long imported everything.
# google.auth.default is mocked like in ViURTestCase.setUp; the App Engine testbed is left out on purpose,
# it imports the blobstore, images and memcache stubs itself. requests.get answers every metadata request
# (region lookup, logging resource detection) with 404, so nothing leaves the machine.
SCRIPT = textwrap.dedent("""
    import json, sys
    from unittest import mock
    import google.auth
    import requests
    google.auth.default = mock.Mock(return_value=(mock.Mock(), "test-project"))
    not_found = requests.Response()
    not_found.status_code = 404
    with mock.patch("google.cloud.storage.Client"), mock.patch("requests.get", return_value=not_found):
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
        result = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)
        loaded = set(json.loads(result.stdout.splitlines()[-1]))
        self.assertEqual([], [name for name in LAZY_MODULES if name in loaded])

    def test_moved_names_stay_importable(self):
        with mock.patch("google.cloud.storage.Client"):
            importlib.import_module("viur.core.modules.file")

        from google.appengine.api import blobstore, images
        from google.cloud import bigquery, recaptchaenterprise_v1
        import jsonschema
        import qrcode
        import qrcode.image.svg
        import user_agents

        for module, name, expected in (
            ("viur.core.modules.file", "blobstore", blobstore),
            ("viur.core.modules.file", "images", images),
            ("viur.core.modules.history", "bigquery", bigquery),
            ("viur.core.modules.user", "user_agents", user_agents),
            ("viur.core.bones.captcha", "recaptchaenterprise_v1", recaptchaenterprise_v1),
            ("viur.core.bones.captcha", "Assessment", recaptchaenterprise_v1.Assessment),
            ("viur.core.bones.json", "jsonschema", jsonschema),
            ("viur.core.render.html.env.viur", "qrcode_make", qrcode.make),
            ("viur.core.render.html.env.viur", "qrcode_svg", qrcode.image.svg),
        ):
            with self.subTest(module=module, name=name):
                self.assertIs(expected, getattr(importlib.import_module(module), name))

        with self.assertRaises(AttributeError):
            importlib.import_module("viur.core.modules.history").does_not_exist
