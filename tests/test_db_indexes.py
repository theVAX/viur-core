"""The equality fields of a Mongo filter, the prefix an index needs, also next to OR groups."""
from unittest import mock

from abstract import ViURTestCase

OR_GROUP = [[("current_kind =", "ticket"), ("current_kind =", "project")]]


class TestEqualityFields(ViURTestCase):

    def test_plain_filter(self):
        from viur.core.db.indexes import equality_fields
        from viur.core.db.utils import to_mongo_filter
        self.assertEqual({"current_key"}, equality_fields(to_mongo_filter({"current_key =": "k", "n >": 1}, [])))

    def test_next_to_an_or_group(self):
        from viur.core.db.indexes import equality_fields
        from viur.core.db.utils import to_mongo_filter
        flt = to_mongo_filter({"current_key =": "k"}, OR_GROUP)
        self.assertIn("$and", flt)
        self.assertEqual({"current_key"}, equality_fields(flt))

    def test_next_to_two_or_groups(self):
        from viur.core.db.indexes import equality_fields
        from viur.core.db.utils import to_mongo_filter
        flt = to_mongo_filter({"current_key =": "k", "a =": 1}, [*OR_GROUP, [("n >", 1), ("n <", 0)]])
        self.assertEqual({"current_key", "a"}, equality_fields(flt))

    def test_an_explicit_eq_next_to_another_operator(self):
        from viur.core.db.indexes import equality_fields
        from viur.core.db.utils import to_mongo_filter
        flt = to_mongo_filter({"current_key =": "k", "current_key IN": ["k", "x"]}, [])
        self.assertEqual({"current_key": {"$eq": "k", "$in": ["k", "x"]}}, flt)
        self.assertEqual({"current_key"}, equality_fields(flt))
        self.assertEqual({"current_key"}, equality_fields(to_mongo_filter(
            {"current_key =": "k", "current_key IN": ["k", "x"]}, OR_GROUP)))

    def test_an_or_group_contributes_nothing(self):
        from viur.core.db.indexes import equality_fields
        from viur.core.db.utils import to_mongo_filter
        self.assertEqual(frozenset(), equality_fields(to_mongo_filter({}, OR_GROUP)))
        self.assertEqual(frozenset(), equality_fields(to_mongo_filter({}, [*OR_GROUP, *OR_GROUP])))


class TestSortElisionNextToAnOrGroup(ViURTestCase):

    def test_the_index_with_the_equality_prefix_is_hinted(self):
        self._assert_hinted({"current_key =": "k"})

    def test_the_index_with_the_equality_prefix_is_hinted_for_eq_next_to_another_operator(self):
        self._assert_hinted({"current_key =": "k", "current_key IN": ["k", "x"]})

    def _assert_hinted(self, filters: dict):
        from viur.core.db import indexes, transport
        from viur.core.db.types import QueryDefinition, QueryOrder, SortOrder

        spec = (("current_key", 1), ("timestamp", -1), ("_id", -1))
        query = QueryDefinition(
            "viur-history", filters, [QueryOrder("timestamp", SortOrder.Descending)], or_filters=OR_GROUP,
        )
        collection = mock.MagicMock()
        cursor = collection.find.return_value.hint.return_value.limit.return_value
        cursor.__iter__.return_value = iter([])
        with (
            mock.patch.object(transport, "get_collection", return_value=collection),
            mock.patch.object(indexes, "existing", return_value=[spec]),
        ):
            transport.run_single_filter(query, 10, False)

        collection.find.return_value.hint.assert_called_once_with(list(spec))
        collection.find.return_value.sort.assert_not_called()
