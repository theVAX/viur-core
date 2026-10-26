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
    """A second ``>=`` on a field used to turn both bounds into one list value (``{"$gte": [1, 5]}``)."""

    def test_a_second_bound_with_the_same_operator_is_refused(self):
        from viur.core import db
        for op in ("<", "<=", ">", ">="):
            with self.subTest(op=op):
                query = db.Query("thing").filter(f"n {op}", 1)
                with self.assertRaises(ValueError):
                    query.filter(f"n {op}", 5)

    def test_a_range_from_two_different_operators_still_works(self):
        from viur.core import db
        query = db.Query("thing").filter("n >=", 1).filter("n <", 5)
        self.assertEqual({"n >=": 1, "n <": 5}, query.queries.filters)
