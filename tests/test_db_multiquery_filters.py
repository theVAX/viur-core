"""A MultiQuery (``SpatialBone``, ``RandomSliceBone``, a key list) must not lose a repeated filter either."""
from abstract import ViURTestCase


def _multi_query():
    from viur.core import db
    query = db.Query("thing")
    query.queries = [db.QueryDefinition("thing", {"x =": 1}, []), db.QueryDefinition("thing", {"x =": 2}, [])]
    return query


class TestRepeatedFilterOnAMultiQuery(ViURTestCase):
    """A single query refuses a second ``IN``, ``!=`` or ``NOT_IN`` on a field; a MultiQuery appended it to a list,
    so a second ``!=`` became ``{"$ne": ["secret", "other"]}`` and let both excluded values through again."""

    def test_a_second_filter_with_the_same_operator_is_refused(self):
        for op, first, second in (("!=", "secret", "other"), ("NOT_IN", ["a"], ["b"]), ("IN", ["a", "b"], ["b"])):
            with self.subTest(op=op):
                query = _multi_query().filter(f"f {op}", first)
                with self.assertRaises(ValueError):
                    query.filter(f"f {op}", second)

    def test_a_single_filter_reaches_every_sub_query(self):
        query = _multi_query().filter("f !=", "secret").filter("f NOT_IN", ["a"])
        for qd in query.queries:
            self.assertEqual("secret", qd.filters["f !="])
            self.assertEqual(["a"], qd.filters["f NOT_IN"])
