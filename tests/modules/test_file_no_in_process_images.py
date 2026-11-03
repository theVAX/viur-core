import subprocess
import sys
import textwrap
from unittest import mock

from abstract import ViURTestCase

# Runs in a fresh interpreter, because this process may have imported Pillow long ago (e.g. via the testbed).
# `sys.modules["PIL"] = None` makes every `import PIL...` fail; google.auth.default and the metadata lookups are
# mocked like in test_lazy_imports.
SCRIPT = textwrap.dedent("""
    import sys
    from unittest import mock
    import google.auth
    import requests
    sys.modules["PIL"] = None
    google.auth.default = mock.Mock(return_value=(mock.Mock(), "test-project"))
    not_found = requests.Response()
    not_found.status_code = 404
    with mock.patch("google.cloud.storage.Client"), mock.patch("requests.get", return_value=not_found):
        import viur.core
        import viur.core.modules.file
""")


class TestNoInProcessImages(ViURTestCase):
    """The app process never decodes an uploaded image; thumbnails and image sizes come from outside."""

    def test_file_module_works_without_pillow(self):
        result = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_in_process_thumbnailer_is_gone(self):
        with mock.patch("google.cloud.storage.Client"):
            from viur.core.modules import file as file_module

        self.assertFalse(hasattr(file_module, "thumbnailer"))
        self.assertFalse(hasattr(file_module.File, "set_image_meta"))
        self.assertTrue(callable(file_module.cloudfunction_thumbnailer))

    def test_added_image_is_not_read(self):
        with mock.patch("google.cloud.storage.Client"):
            from viur.core.modules import file as file_module

        files = object.__new__(file_module.File)  # no module tree needed for this hook
        files.skel_type_of = lambda skel: "leaf"
        files.get_bucket = mock.Mock()
        files.set_image_meta = mock.Mock()  # the former deferred size lookup, which downloaded and opened the image
        skel = mock.MagicMock(kindName="file")
        skel.__getitem__.side_effect = {"key": "file-key", "mimetype": "image/png", "size": 100}.get
        with mock.patch("viur.core.prototypes.tree.flushCache"):
            files.thenAdd(skel)
        files.get_bucket.assert_not_called()
        files.set_image_meta.assert_not_called()
