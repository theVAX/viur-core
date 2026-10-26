"""
Tests for Script.getAvailableRootNodes:
  The scripts root node is offered (and created) only for users who may list scripts,
  i.e. with "script-view" or "root" by default.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from abstract import ViURTestCase  # noqa: E402


class TestScriptRootNodes(ViURTestCase):

    def setUp(self):
        super().setUp()
        with mock.patch("google.cloud.storage.Client"):  # modules.file, imported by script, builds one at import
            from viur.core.modules.script import Script

        self.script = object.__new__(Script)
        self.script.moduleName = "script"
        self.rootnode = mock.patch.object(Script, "rootnodeSkel", return_value={"key": "rep_module_repo"}).start()
        self.addCleanup(mock.patch.stopall)

    def _root_nodes_for(self, user: dict | None) -> list:
        from viur.core import current
        current.user.set(user)
        return self.script.getAvailableRootNodes()

    def test_denied_without_script_view(self):
        for user in (None, {"access": []}, {"access": ["admin", "file-view", "script-edit"]}):
            with self.subTest(user=user):
                self.assertEqual(self._root_nodes_for(user), [])
        self.rootnode.assert_not_called()  # nothing is created for them either

    def test_offered_with_script_view_or_root(self):
        for access in (["admin", "script-view"], ["root"]):
            with self.subTest(access=access):
                self.assertEqual(
                    self._root_nodes_for({"access": access}),
                    [{"name": "Scripts", "key": "rep_module_repo"}],
                )
