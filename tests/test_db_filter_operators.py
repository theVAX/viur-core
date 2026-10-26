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

    def test_a_range_from_two_different_operators_still_works(self):
        from viur.core import db
        query = db.Query("thing").filter("n >=", 1).filter("n <", 5)
        self.assertEqual({"n >=": 1, "n <": 5}, query.queries.filters)
