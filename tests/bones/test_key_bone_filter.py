"""A key filter from the request must not replace a restriction already on the query."""
from abstract import ViURTestCase


def _skel():
    """A minimal SkeletonInstance with a KeyBone, without a Skeleton class on disk."""
    from viur.core.bones import KeyBone
    from viur.core.skeleton import SkeletonInstance

    class FakeSkelCls:
        kindName = "thing"
        __boneMap__ = {}

    bone = KeyBone()
    bone.__set_name__(FakeSkelCls, "key")
    return SkeletonInstance(FakeSkelCls, bone_map={"key": bone})


class TestKeyListKeepsRestriction(ViURTestCase):
    """``key=[...]`` builds one sub-query per key; each used to overwrite an existing ``_id =`` restriction."""

    def test_a_key_list_keeps_an_id_restriction(self):
        from viur.core import db
        query = db.Query("thing", _skel()).filter("_id =", "allowed")  # e.g. a listFilter applied first
        query.mergeExternalFilter({"key": ["forbidden", "allowed"]})
        self.assertEqual([["allowed", "forbidden"], ["allowed", "allowed"]],
                         [qd.filters["_id ="] for qd in query.queries])

    def test_a_key_list_keeps_an_id_in_restriction(self):
        """Both conditions stay in every sub-query; ``to_mongo_filter`` ANDs ``=`` with ``IN`` only together with
        fix/db-filter-operators (V-037), before that the ``IN`` is lost on the way to MongoDB."""
        from viur.core import db
        query = db.Query("thing", _skel()).filter("_id IN", ["allowed"])
        query.mergeExternalFilter({"key": ["forbidden"]})
        self.assertEqual([{"_id IN": ["allowed"], "_id =": "forbidden"}], [qd.filters for qd in query.queries])

    def test_a_key_list_with_a_prefix_keeps_the_restriction(self):
        from viur.core import db
        skel = _skel()
        query = db.Query("viur-relations").filter("dest._id =", "allowed")
        skel.key.buildDBFilter("key", skel, query, {"key": ["forbidden"]}, prefix="dest.")
        self.assertEqual([["allowed", "forbidden"]], [qd.filters["dest._id ="] for qd in query.queries])

    def test_a_key_list_without_a_restriction(self):
        from viur.core import db
        query = db.Query("thing", _skel()).mergeExternalFilter({"key": ["a", "b"]})
        self.assertEqual(["a", "b"], [qd.filters["_id ="] for qd in query.queries])

    def test_a_single_key_keeps_an_id_restriction(self):
        from viur.core import db
        query = db.Query("thing", _skel()).filter("_id =", "allowed")
        query.mergeExternalFilter({"key": "forbidden"})
        self.assertEqual(["allowed", "forbidden"], query.queries.filters["_id ="])
