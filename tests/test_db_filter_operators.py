"""Several operators on the same field must all reach MongoDB; none may silently replace another."""
from abstract import ViURTestCase


class TestEqualityNextToOtherOperators(ViURTestCase):
    """``=`` next to a further operator on the same field, as a ``listFilter`` plus a request filter produce it."""

    CASES = [
        ("IN", ["a", "b"], "$in"),
        ("NOT_IN", ["b"], "$nin"),
        ("!=", "b", "$ne"),
        ("<", "z", "$lt"),
        (">=", "a", "$gte"),
    ]

    def test_equality_first(self):
        from viur.core.db.utils import to_mongo_filter
        for op, value, mongo_op in self.CASES:
            with self.subTest(op=op):
                self.assertEqual(
                    {"_id": {"$eq": "a", mongo_op: value}},
                    to_mongo_filter({"_id =": "a", f"_id {op}": value}, []),
                )

    def test_equality_last(self):
        from viur.core.db.utils import to_mongo_filter
        for op, value, mongo_op in self.CASES:
            with self.subTest(op=op):
                self.assertEqual(
                    {"_id": {mongo_op: value, "$eq": "a"}},
                    to_mongo_filter({f"_id {op}": value, "_id =": "a"}, []),
                )

    def test_equality_alone_stays_a_plain_value(self):
        from viur.core.db.utils import to_mongo_filter
        self.assertEqual({"name": "a", "n": 5}, to_mongo_filter({"name =": "a", "n =": 5}, []))

    def test_equality_alone_inside_an_or_group_stays_a_plain_value(self):
        from viur.core.db.utils import to_mongo_filter
        self.assertEqual({"$or": [{"name": "a"}, {"name": "b"}]},
                         to_mongo_filter({}, [[("name =", "a"), ("name =", "b")]]))

    def test_a_key_list_from_the_request_next_to_an_id_in_restriction(self):
        """``KeyBone`` puts ``_id =`` into every sub-query next to the ``_id IN`` of a ``listFilter``."""
        from viur.core import db
        from viur.core.bones import KeyBone
        from viur.core.db.utils import to_mongo_filter
        from viur.core.skeleton import SkeletonInstance

        class FakeSkelCls:
            kindName = "thing"
            __boneMap__ = {}

        bone = KeyBone()
        bone.__set_name__(FakeSkelCls, "key")
        query = db.Query("thing", SkeletonInstance(FakeSkelCls, bone_map={"key": bone})).filter("_id IN", ["allowed"])
        query.mergeExternalFilter({"key": ["forbidden"]})
        self.assertEqual([{"_id": {"$in": ["allowed"], "$eq": "forbidden"}}],
                         [to_mongo_filter(qd.filters, qd.or_filters) for qd in query.queries])

    def test_repeated_equality_next_to_another_operator(self):
        from viur.core.db.utils import to_mongo_filter
        self.assertEqual({"tags": {"$all": ["a", "b"], "$nin": ["c"]}},
                         to_mongo_filter({"tags =": ["a", "b"], "tags NOT_IN": ["c"]}, []))


class TestSameRangeOperatorTwice(ViURTestCase):
    """A second ``>=`` on a field used to turn both bounds into one list value (``{"$gte": [1, 5]}``).

    Both bounds have to hold, e.g. one from ``listFilter`` and one from the request.
    """

    OPS = {"<": "$lt", "<=": "$lte", ">": "$gt", ">=": "$gte"}

    def test_both_bounds_are_kept(self):
        from viur.core import db
        from viur.core.db.utils import to_mongo_filter
        for op, mongo_op in self.OPS.items():
            with self.subTest(op=op):
                qd = db.Query("thing").filter(f"n {op}", 1).filter(f"n {op}", 5).queries
                self.assertEqual({"$and": [{"n": {mongo_op: 1}}, {"n": {mongo_op: 5}}]},
                                 to_mongo_filter(qd.filters, qd.or_filters))

    def test_both_bounds_next_to_other_filters_and_an_or_group(self):
        from viur.core.db.utils import to_mongo_filter
        self.assertEqual(
            {"$and": [{"a": 1, "n": {"$lt": 9}}, {"n": {"$gte": 1}}, {"n": {"$gte": 5}}, {"$or": [{"b": 2}]}]},
            to_mongo_filter({"a =": 1, "n >=": [1, 5], "n <": 9}, [[("b =", 2)]]),
        )

    def test_both_bounds_are_kept_in_every_sub_query_of_a_multi_query(self):
        from viur.core import db
        from viur.core.db.utils import to_mongo_filter
        query = db.Query("thing")
        query.queries = [db.QueryDefinition("thing", {"x =": 1}, []), db.QueryDefinition("thing", {"x =": 2}, [])]
        query.filter("n >=", 1).filter("n >=", 5)
        for qd in query.queries:
            self.assertEqual({"$and": [{"x": qd.filters["x ="]}, {"n": {"$gte": 1}}, {"n": {"$gte": 5}}]},
                             to_mongo_filter(qd.filters, qd.or_filters))

    def test_a_client_side_match_checks_both_bounds(self):
        """``_entryMatchesQuery`` (the post-filter of a fulltext search) reads the same filters."""
        from viur.core.db.query import _entryMatchesQuery
        self.assertFalse(_entryMatchesQuery({"n": 3}, {"n >=": [1, 5]}))
        self.assertTrue(_entryMatchesQuery({"n": 7}, {"n >=": [1, 5]}))

    def test_an_empty_list_of_bounds_is_unsatisfiable(self):
        """Like ``$all: []`` for ``=``; dropping the condition would match every document instead."""
        from viur.core.db.utils import to_mongo_filter
        self.assertEqual({"n": {"$in": []}}, to_mongo_filter({"n >=": []}, []))
        self.assertEqual({"$and": [{"a": 1}, {"n": {"$in": []}}]}, to_mongo_filter({"a =": 1, "n <": []}, []))

    def test_a_range_from_two_different_operators_still_works(self):
        from viur.core import db
        query = db.Query("thing").filter("n >=", 1).filter("n <", 5)
        self.assertEqual({"n >=": 1, "n <": 5}, query.queries.filters)


def _fulltext_query(hits: list[dict]):
    """A fulltext query whose adapter returns *hits* and leaves the query's constraints to the post-filter.

    The post-filter (``_entryMatchesQuery``) runs in ``Query.run()`` when the adapter does not guarantee them.
    """
    from types import SimpleNamespace
    from viur.core import db
    from viur.core.bones import KeyBone
    from viur.core.skeleton import SkeletonInstance

    adapter = SimpleNamespace(providesFulltextSearch=True, fulltextSearchGuaranteesQueryConstrains=False,
                              fulltextSearch=lambda query_string, query: list(hits))

    class FakeSkelCls:
        kindName = "thing"
        __boneMap__ = {}
        database_adapters = [adapter]

    bone = KeyBone()
    bone.__set_name__(FakeSkelCls, "key")
    return db.Query("thing", SkeletonInstance(FakeSkelCls, bone_map={"key": bone})).mergeExternalFilter(
        {"search": "term"})


class TestRepeatedEqualityClientSide(ViURTestCase):
    """A repeated ``=`` arrives as a list and means AND (``$all`` in MongoDB); the fulltext post-filter must agree."""

    def _run_fulltext(self, filters: dict, hits: list[dict], restriction: tuple[str, str] | None = None) -> list:
        query = _fulltext_query(hits)
        if restriction:
            query.filter(*restriction)
        return query.mergeExternalFilter(filters).run()

    def test_a_key_from_the_request_equal_to_an_id_restriction_keeps_the_allowed_hit(self):
        hits = [{"_id": "allowed"}, {"_id": "forbidden"}]
        self.assertEqual([{"_id": "allowed"}],
                         self._run_fulltext({"key": "allowed"}, hits, restriction=("_id =", "allowed")))

    def test_a_key_from_the_request_other_than_an_id_restriction_matches_nothing(self):
        hits = [{"_id": "allowed"}, {"_id": "forbidden"}]
        self.assertEqual([], self._run_fulltext({"key": "forbidden"}, hits, restriction=("_id =", "allowed")))

    def test_a_repeated_equality_on_a_multi_value_field_needs_every_value(self):
        from viur.core.db.query import _entryMatchesQuery
        self.assertTrue(_entryMatchesQuery({"tags": ["a", "b", "c"]}, {"tags =": ["a", "b"]}))
        self.assertFalse(_entryMatchesQuery({"tags": ["a", "c"]}, {"tags =": ["a", "b"]}))
        self.assertFalse(_entryMatchesQuery({"tags": "a"}, {"tags =": ["a", "b"]}))

    def test_an_empty_list_of_values_matches_nothing(self):
        """``$all: []`` matches nothing in MongoDB, and an empty list of bounds is unsatisfiable as well."""
        hits = [{"_id": "allowed", "n": 1}, {"_id": "forbidden", "n": 5}]
        for condition in (("_id =", []), ("n >=", [])):
            with self.subTest(condition=condition):
                self.assertEqual([], _fulltext_query(hits).filter(*condition).run())

    def test_an_empty_list_of_values_matches_nothing_in_a_multi_query(self):
        from viur.core import db

        def multi_query(hits):
            query = _fulltext_query(hits)
            query.queries = [db.QueryDefinition("thing", {"x =": 1}, []), db.QueryDefinition("thing", {"x =": 2}, [])]
            return query

        hits = [{"_id": "a", "x": 1}, {"_id": "b", "x": 2}]
        self.assertEqual(hits, multi_query(hits).run())
        self.assertEqual([], multi_query(hits).filter("_id =", []).run())


class TestListValuesInOrGroupsClientSide(ViURTestCase):
    """An OR branch reads a list value like a plain filter does: AND for ``=`` and range bounds, as in MongoDB."""

    def _ids(self, hits: list[dict], *conditions: tuple[str, object]) -> list[str]:
        return [hit["_id"] for hit in _fulltext_query(hits).or_filter(*conditions).run()]

    def test_a_repeated_equality_in_an_or_branch_needs_every_value(self):
        hits = [{"_id": "both", "tags": ["a", "b"]}, {"_id": "one", "tags": ["a"]}, {"_id": "other", "tags": ["c"]}]
        self.assertEqual(["both"], self._ids(hits, ("tags =", ["a", "b"])))
        self.assertEqual(["both", "other"], self._ids(hits, ("tags =", ["a", "b"]), ("tags =", "c")))

    def test_several_bounds_in_an_or_branch_must_all_hold(self):
        hits = [{"_id": "seven", "n": 7}, {"_id": "three", "n": 3}]
        self.assertEqual(["seven"], self._ids(hits, ("n >=", [1, 5])))

    def test_an_empty_list_in_an_or_branch_matches_nothing(self):
        hits = [{"_id": "a", "tags": ["a"], "n": 1}]
        for condition in (("tags =", []), ("n >=", [])):
            with self.subTest(condition=condition):
                self.assertEqual([], self._ids(hits, condition))

    def test_the_translation_for_mongodb_agrees(self):
        from viur.core.db.utils import to_mongo_filter
        self.assertEqual({"$or": [{"tags": {"$all": ["a", "b"]}}, {"tags": "c"}]},
                         to_mongo_filter({}, [[("tags =", ["a", "b"]), ("tags =", "c")]]))
        self.assertEqual({"$or": [{"$and": [{"n": {"$gte": 1}}, {"n": {"$gte": 5}}]}]},
                         to_mongo_filter({}, [[("n >=", [1, 5])]]))
