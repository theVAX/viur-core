"""
Tests for the scripts root node:
  getAvailableRootNodes, listRootNodes and index offer (and create) it only for users who may list
  scripts, i.e. with "script-view" or "root" by default; get_importable refuses everyone else before
  it touches the root node.
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

    def test_list_root_nodes_and_index_refuse_without_script_view(self):
        from viur.core import current, errors
        current.user.set({"access": ["admin"]})
        self.script.render = mock.MagicMock()

        self.script.listRootNodes()
        self.script.render.listRootNodes.assert_called_once_with([])

        with self.assertRaises(errors.Unauthorized):
            self.script.index()
        self.rootnode.assert_not_called()

    def test_get_importable_refuses_before_touching_the_root_node(self):
        from viur.core import current, errors
        for user in (None, {"access": ["admin"]}):
            with self.subTest(user=user):
                current.user.set(user)
                with self.assertRaises(errors.Unauthorized):
                    self.script.get_importable()
        self.rootnode.assert_not_called()

    def test_get_importable_looks_below_the_root_node(self):
        from viur.core import current, db, errors
        current.user.set({"access": ["script-view"]})
        with mock.patch.object(db.Query, "getEntry", autospec=True, return_value=None) as get_entry:
            with self.assertRaises(errors.NotFound):
                self.script.get_importable()
        filters = get_entry.call_args.args[0].queries.filters
        self.assertEqual((filters["parententry ="], filters["name ="]), ("rep_module_repo", "importable"))
