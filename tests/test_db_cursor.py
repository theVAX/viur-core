"""Paging a list: the cursor of one page has to open the next one."""
import operator
from unittest import mock

from abstract import ViURTestCase


def _src_skel():
    """A minimal SkeletonInstance for ``Query.mergeExternalFilter``, without a Skeleton class on disk."""
    from viur.core.bones import KeyBone, StringBone
    from viur.core.skeleton import SkeletonInstance

    class FakeSkelCls:
        kindName = "thing"
        __boneMap__ = {}

    bones = {"key": KeyBone(), "name": StringBone(), "owner": StringBone()}
    for name, bone in bones.items():
        bone.__set_name__(FakeSkelCls, name)
    return SkeletonInstance(FakeSkelCls, bone_map=bones)


class TestListCursor(ViURTestCase):
    """``List.list`` merges the request (with its cursor) first, then ``listFilter`` and the default order add to
    the query. The cursor is bound to the query it was issued for, so it must be checked against the final one."""

    def _list_page(self, kwargs: dict, owner: str = "me", returns: list[dict] = ()):
        """Run one list page the way ``List.list`` builds it.

        :return: The query and the ``QueryDefinition`` that reached the transport.
        """
        from viur.core import db

        seen = []

        def run_single_filter(qd, limit, keys_only, **kw):
            seen.append(qd)
            qd.currentCursor = {"name": returns[-1]["name"], "_id": returns[-1]["_id"]} if returns else None
            return list(returns)

        query = db.Query("thing", _src_skel()).mergeExternalFilter(kwargs)
        query.filter("owner =", owner)  # listFilter
        query.order("name")  # _apply_default_order
        with mock.patch("viur.core.db.query.run_single_filter", side_effect=run_single_filter):
            query.run()
        return query, seen[0]

    def test_the_cursor_opens_the_next_page(self):
        query, _ = self._list_page({"limit": "2"}, returns=[{"_id": "1", "name": "a"}, {"_id": "2", "name": "b"}])
        cursor = query.getCursor()
        self.assertIsNotNone(cursor)

        _, qd = self._list_page({"limit": "2", "cursor": cursor})

        self.assertEqual({"name": "b", "_id": "2"}, qd.startCursor)

    def test_a_cursor_of_another_list_filter_is_ignored(self):
        query, _ = self._list_page({}, owner="someone-else", returns=[{"_id": "1", "name": "a"}])

        with self.assertLogs(level="WARNING") as logs:
            _, qd = self._list_page({"cursor": query.getCursor()})

        self.assertIsNone(qd.startCursor)
        self.assertIn("Ignoring invalid cursor", "\n".join(logs.output))

    def test_a_broken_cursor_is_ignored(self):
        with self.assertLogs(level="WARNING"):
            _, qd = self._list_page({"cursor": "not-a-cursor"})
        self.assertIsNone(qd.startCursor)

    def test_a_clone_keeps_the_cursor(self):
        from viur.core import db

        query, _ = self._list_page({}, returns=[{"_id": "1", "name": "a"}])
        clone = db.Query("thing", _src_skel()).mergeExternalFilter({"cursor": query.getCursor()}).clone()
        clone.filter("owner =", "me").order("name")
        with mock.patch("viur.core.db.query.run_single_filter", return_value=[]) as run_single_filter:
            clone.run()
        self.assertEqual({"name": "a", "_id": "1"}, run_single_filter.call_args.args[0].startCursor)

    def test_a_later_set_cursor_wins(self):
        """``Pagination`` runs a query, then moves it on with ``setCursor`` and runs it again."""
        from viur.core import db

        first, _ = self._list_page({}, returns=[{"_id": "1", "name": "a"}])
        second, _ = self._list_page({}, returns=[{"_id": "2", "name": "b"}])
        query = db.Query("thing", _src_skel()).mergeExternalFilter({"cursor": first.getCursor()})
        query.filter("owner =", "me").order("name")
        with mock.patch("viur.core.db.query.run_single_filter", return_value=[]) as run_single_filter:
            query.run()
            query.setCursor(second.getCursor())
            query.run()
        self.assertEqual({"name": "b", "_id": "2"}, run_single_filter.call_args.args[0].startCursor)


    def test_an_explicit_set_cursor_wins_over_the_request(self):
        """Before the cursor was deferred, ``setCursor`` after ``mergeExternalFilter`` replaced the request's one."""
        from viur.core import db

        first, _ = self._list_page({}, returns=[{"_id": "1", "name": "a"}])
        second, _ = self._list_page({}, returns=[{"_id": "2", "name": "b"}])
        query = db.Query("thing", _src_skel()).mergeExternalFilter({"cursor": first.getCursor()})
        query.filter("owner =", "me").order("name")
        query.setCursor(second.getCursor())
        with mock.patch("viur.core.db.query.run_single_filter", return_value=[]) as run_single_filter:
            query.run()
        self.assertEqual({"name": "b", "_id": "2"}, run_single_filter.call_args.args[0].startCursor)


class TestLimitOneCursor(ViURTestCase):
    """A list page with ``limit=1`` needs a cursor like any other page; only ``getEntry()`` may skip the sort."""

    def _run(self, call):
        from viur.core.db import transport

        with mock.patch.object(transport, "_find", return_value=[{"_id": "a"}]) as find:
            query = call()
        return query, find.call_args.args[2]

    def test_a_limit_one_page_is_sorted_and_issues_a_cursor(self):
        from viur.core import db

        def call():
            query = db.Query("thing")
            query.run(1)
            return query

        query, sort = self._run(call)
        self.assertEqual([("_id", 1)], sort)
        self.assertIsNotNone(query.getCursor())

    def test_get_entry_sends_no_sort(self):
        from viur.core import db

        def call():
            query = db.Query("thing")
            self.assertEqual({"_id": "a"}, query.getEntry())
            return query

        _, sort = self._run(call)
        self.assertEqual([], sort)

    def test_get_entry_keeps_an_explicit_order(self):
        from viur.core import db

        def call():
            query = db.Query("thing").order("name")
            query.getEntry()
            return query

        _, sort = self._run(call)
        self.assertEqual([("name", 1), ("_id", 1)], sort)


class TestQueryOverrides(ViURTestCase):
    """Subclasses that override ``run`` or ``_run_single_filter_query`` with the old signature keep working."""

    def test_an_old_run_single_filter_query_override(self):
        from viur.core import db

        class OldQuery(db.Query):
            def _run_single_filter_query(self, query, limit, keys_only):
                return [{"_id": "a"}]

        self.assertEqual([{"_id": "a"}], OldQuery("thing").run(5))
        self.assertEqual({"_id": "a"}, OldQuery("thing").getEntry())

    def test_an_old_run_override(self):
        from viur.core import db

        class OldQuery(db.Query):
            def run(self, limit=-1, keys_only=False):
                return [{"_id": "a"}]

        self.assertEqual({"_id": "a"}, OldQuery("thing").getEntry())

    def test_get_entry_does_not_hide_a_type_error(self):
        from viur.core import db

        with (
            mock.patch("viur.core.db.query.run_single_filter", side_effect=TypeError("broken")),
            self.assertRaises(TypeError),
        ):
            db.Query("thing").getEntry()


def _token(after, query_definition) -> str:
    """A cursor token as ``Query.getCursor`` builds it, with a valid query hash."""
    import base64
    from viur.core.db.query import _cursor_hash
    from viur.core.utils import json as vjson
    payload = {"after": after, "q": _cursor_hash(query_definition)}
    return base64.urlsafe_b64encode(vjson.dumps(payload).encode("ASCII")).decode("ASCII")


class TestCursorValues(ViURTestCase):
    """A cursor with the right query hash still has to carry a value for every sort field, ``_id`` included."""

    def _query(self):
        from viur.core import db
        return db.Query("thing").filter("owner =", "me").order("name")

    def test_a_cursor_without_id_is_refused(self):
        query = self._query()
        with self.assertRaises(ValueError):
            query.setCursor(_token({"name": "a"}, query.queries))

    def test_a_cursor_that_is_no_dict_is_refused(self):
        for after in (["a", "1"], "a"):
            with self.subTest(after=after):
                query = self._query()
                with self.assertRaises(ValueError):
                    query.setCursor(_token(after, query.queries))
                with self.assertRaises(ValueError):
                    query.setCursor(None, _token(after, query.queries))

    def test_a_request_cursor_without_id_is_ignored(self):
        from viur.core import db
        query = self._query()
        token = _token({"name": "a"}, query.queries)
        request_query = db.Query("thing", _src_skel()).mergeExternalFilter({"cursor": token})
        request_query.filter("owner =", "me").order("name")
        with (
            mock.patch("viur.core.db.transport._find", return_value=[]) as find,
            self.assertLogs(level="WARNING") as logs,
        ):
            request_query.run()
        self.assertIn("Ignoring invalid cursor", "\n".join(logs.output))
        self.assertEqual({"owner": "me"}, find.call_args.args[1])

    def test_a_complete_cursor_is_accepted(self):
        query = self._query()
        query.setCursor(_token({"name": "a", "_id": "1"}, query.queries))
        self.assertEqual({"name": "a", "_id": "1"}, query.queries.startCursor)


    def test_a_cursor_value_bson_cannot_encode_is_refused(self):
        """An int beyond 64 bit passed the checks and raised OverflowError when the filter was encoded."""
        query = self._query()
        with self.assertRaises(ValueError):
            query.setCursor(_token({"name": "a", "_id": 2 ** 80}, query.queries))

    def test_an_undecodable_cursor_is_refused(self):
        """A broken decimal marker raised decimal.InvalidOperation while decoding, before any hash check."""
        import base64
        query = self._query()
        payload = '{"after": {".__decimal__": "oops"}, "q": "x"}'
        token = base64.urlsafe_b64encode(payload.encode("ASCII")).decode("ASCII")
        with self.assertRaises(ValueError):
            query.setCursor(token)

    def test_crafted_request_cursors_are_ignored(self):
        import base64
        from viur.core import db
        shape = self._query().queries
        broken = '{"after": {".__decimal__": "oops"}, "q": "x"}'
        for token in (_token({"name": "a", "_id": 2 ** 80}, shape),
                      base64.urlsafe_b64encode(broken.encode("ASCII")).decode("ASCII")):
            with self.subTest(token=token):
                query = db.Query("thing", _src_skel()).mergeExternalFilter({"cursor": token})
                query.filter("owner =", "me").order("name")
                with (
                    mock.patch("viur.core.db.transport._find", return_value=[]) as find,
                    self.assertLogs(level="WARNING") as logs,
                ):
                    query.run()
                self.assertIn("Ignoring invalid cursor", "\n".join(logs.output))
                self.assertEqual({"owner": "me"}, find.call_args.args[1])


class TestQueryIterCursors(ViURTestCase):
    """``QueryIter`` hands the query to deferred tasks; its cursors must survive that round trip."""

    def _steps(self, query, batches: list[list[dict]]):
        """Run ``startIterOnQuery`` and the following ``_qryStep`` calls with a JSON round trip in between.

        :return: The ``(startCursor, endCursor)`` of every query that reached the transport.
        """
        from viur.core import tasks, utils

        payloads, seen = [], []

        def run_single_filter(qd, limit, keys_only, **kw):
            seen.append((qd.startCursor, qd.endCursor))
            docs = batches.pop(0) if batches else []
            qd.currentCursor = {"name": docs[-1]["name"], "_id": docs[-1]["_id"]} if docs else None
            return docs

        with (
            mock.patch.object(tasks.QueryIter, "_requeueStep",
                              side_effect=lambda qry_dict: payloads.append(utils.json.dumps(qry_dict))),
            mock.patch.object(tasks.QueryIter, "handleEntry"),
            mock.patch.object(tasks.QueryIter, "handleFinish"),
            mock.patch("viur.core.db.query.run_single_filter", side_effect=run_single_filter),
        ):
            tasks.QueryIter.startIterOnQuery(query)
            while payloads:
                tasks.QueryIter._qryStep(utils.json.loads(payloads.pop(0)))
        return seen

    def test_the_cursors_of_the_request_reach_the_first_step(self):
        from viur.core import db

        shape = db.Query("thing").filter("owner =", "me").order("name").queries
        start, end = _token({"name": "a", "_id": "1"}, shape), _token({"name": "x", "_id": "9"}, shape)
        query = db.Query("thing", _src_skel()).mergeExternalFilter({"cursor": start, "endcursor": end})
        query.filter("owner =", "me").order("name")
        query.srcSkel = None  # the stand-in skeleton is not registered under its kind

        seen = self._steps(query, [[{"_id": "2", "name": "b"}]])

        self.assertEqual(({"name": "a", "_id": "1"}, {"name": "x", "_id": "9"}), seen[0])
        self.assertEqual(({"name": "b", "_id": "2"}, {"name": "x", "_id": "9"}), seen[1])


class _FakeCollection:
    """Just enough of a pymongo collection for keyset paging: ``find``/``sort``/``limit`` over plain dicts."""

    _OPS = {"$gt": operator.gt, "$gte": operator.ge, "$lt": operator.lt, "$lte": operator.le}

    def __init__(self, docs: list[dict]):
        self.docs = docs

    def index_information(self) -> dict:
        return {}

    def find(self, flt: dict, session=None) -> "_FakeCollection":
        return _FakeCollection([doc for doc in self.docs if self._matches(doc, flt)])

    def sort(self, sort: list[tuple[str, int]]) -> "_FakeCollection":
        docs = list(self.docs)
        for field, direction in reversed(sort):
            docs.sort(key=lambda doc: doc[field], reverse=direction == -1)
        return _FakeCollection(docs)

    def limit(self, limit: int) -> "_FakeCollection":
        return _FakeCollection(self.docs[:limit])

    def __iter__(self):
        return iter(self.docs)

    @classmethod
    def _matches(cls, doc: dict, flt: dict) -> bool:
        for key, cond in flt.items():
            if key == "$and":
                ok = all(cls._matches(doc, part) for part in cond)
            elif key == "$or":
                ok = any(cls._matches(doc, part) for part in cond)
            elif isinstance(cond, dict):
                value = doc.get(key)
                ok = all(value != arg if op == "$ne" else value is not None and cls._OPS[op](value, arg)
                         for op, arg in cond.items())
            else:
                ok = doc.get(key) == cond
            if not ok:
                return False
        return True


class TestQueryIterInvertedOrder(ViURTestCase):
    """An ``Inverted*`` order reads its start cursor inclusively; a task continuation must not, or it never ends."""

    def _query(self, order, start: dict | None = None, end: dict | None = None):
        """The query by ``name`` in *order*, with cursors on the rows *start* and *end*."""
        from viur.core import db
        query = db.Query("inverted-thing").order(("name", order))
        if start or end:
            query.setCursor(start and _token(start, query.queries), end and _token(end, query.queries))
        return query

    def _iterate(self, docs: list[dict], max_steps: int = 10, order=None, start: dict | None = None,
                 end: dict | None = None) -> tuple[list[str], int]:
        """Iterate *docs* by ``name`` (``InvertedAscending`` by default) through the real transport and a fake
        collection.

        :return: The names handled, in order, plus ``"finish"`` once finished, and the number of steps.
        """
        from viur.core import db, tasks, utils
        from viur.core.db import transport

        handled, payloads = [], []

        class Collect(tasks.QueryIter):
            @classmethod
            def handleEntry(cls, entry, customData, kind):
                handled.append(entry["name"])

            @classmethod
            def handleFinish(cls, totalCount, customData):
                handled.append("finish")

        steps = 0
        with (
            mock.patch.object(tasks.QueryIter, "_requeueStep",
                              side_effect=lambda qry_dict: payloads.append(utils.json.dumps(qry_dict))),
            mock.patch.object(transport, "get_collection", return_value=_FakeCollection(docs)),
        ):
            Collect.startIterOnQuery(self._query(order or db.SortOrder.InvertedAscending, start, end))
            while payloads and steps < max_steps:
                steps += 1
                Collect._qryStep(utils.json.loads(payloads.pop(0)))
        return handled, steps

    def test_one_record(self):
        handled, steps = self._iterate([{"_id": "1", "name": "a"}])
        self.assertEqual(["a", "finish"], handled)
        self.assertLess(steps, 10)

    def test_several_records(self):
        docs = [{"_id": str(i), "name": f"n{i}"} for i in range(12)]
        handled, steps = self._iterate(docs)
        self.assertEqual("finish", handled[-1])
        self.assertEqual(sorted(doc["name"] for doc in docs), sorted(handled[:-1]))
        self.assertLess(steps, 10)

    def test_the_cursors_bound_the_iteration_as_they_bound_the_query(self):
        """The first step reads the start cursor as the query does (inclusively for an ``Inverted*`` order)."""
        from viur.core import db
        from viur.core.db import transport

        docs = [{"_id": str(i), "name": f"n{i:02}"} for i in range(12)]
        row = {i: {"name": doc["name"], "_id": doc["_id"]} for i, doc in enumerate(docs)}
        cases = {
            # InvertedAscending fetches descending, InvertedDescending ascending.
            db.SortOrder.InvertedAscending: [(row[2], None), (row[9], None), (None, row[4]), (row[9], row[2])],
            db.SortOrder.InvertedDescending: [(row[9], None), (row[2], None), (None, row[4]), (row[2], row[9])],
        }
        for order, cursors in cases.items():
            for start, end in cursors:
                with self.subTest(order=order, start=start, end=end):
                    with mock.patch.object(transport, "get_collection", return_value=_FakeCollection(docs)):
                        expected = sorted(doc["name"] for doc in self._query(order, start, end).run(100))
                    handled, steps = self._iterate(docs, order=order, start=start, end=end)
                    self.assertEqual("finish", handled[-1])
                    self.assertEqual(expected, sorted(handled[:-1]))
                    self.assertLess(steps, 10)

    def test_a_start_cursor_on_the_second_row(self):
        """``InvertedAscending`` from the cursor of row 2 shows rows 1 and 2; the iteration handles both."""
        from viur.core import db
        docs = [{"_id": str(i), "name": f"n{i}"} for i in (1, 2, 3)]
        handled, _ = self._iterate(docs, order=db.SortOrder.InvertedAscending, start={"name": "n2", "_id": "2"})
        self.assertEqual(["n1", "n2", "finish"], sorted(handled[:-1]) + handled[-1:])
