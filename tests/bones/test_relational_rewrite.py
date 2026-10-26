"""Rewriting a query onto viur-relations (``RelationalBone`` with ``multiple=True``) must keep its OR groups."""
import types

from abstract import ViURTestCase


def _bone_and_query():
    from viur.core import db
    from viur.core.bones import RelationalBone

    bone = RelationalBone(kind="tag", multiple=True, parentKeys={"owner", "public"})
    bone._ref_keys = {"key", "name"}  # set up by setSystemInitialized() in a running app
    query = db.Query("thing").filter("owner =", "me")
    query.or_filter(("owner =", "me"), ("public =", True))  # an access rule: own or public records
    return bone, query


class TestRelationalRewriteKeepsOrFilters(ViURTestCase):

    def test_the_or_groups_move_onto_the_relation(self):
        bone, query = _bone_and_query()
        bone._rewriteQuery("tags", types.SimpleNamespace(kindName="thing"), query, {})
        self.assertEqual("viur-relations", query.queries.kind)
        self.assertEqual("me", query.queries.filters["src.owner ="])
        self.assertEqual([[("src.owner =", "me"), ("src.public =", True)]], query.queries.or_filters)

    def test_a_sort_by_a_relational_field_keeps_them(self):
        """E.g. a ``default_order`` dict merged after ``listFilter``: the sort rewrites the query."""
        bone, query = _bone_and_query()
        bone.buildDBSort("tags", types.SimpleNamespace(kindName="thing"), query, {"orderby": "tags.dest.name"})
        self.assertEqual([[("src.owner =", "me"), ("src.public =", True)]], query.queries.or_filters)

    def test_an_or_condition_outside_the_parent_keys_is_refused(self):
        bone, query = _bone_and_query()
        query.or_filter(("secret =", 1), ("public =", True))
        with self.assertRaises(RuntimeError):
            bone._rewriteQuery("tags", types.SimpleNamespace(kindName="thing"), query, {})
