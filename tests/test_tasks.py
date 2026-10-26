"""Tests for the dispatching in :mod:`viur.core.tasks`."""
from unittest import mock

from abstract import ViURTestCase


class TestCallDeferredDirectCall(ViURTestCase):
    """``_call_deferred=False`` must run the wrapped function right away.

    Whether a task queue can be reached is a property of the environment and must not
    change that: callers use the parameter precisely because they need the result now,
    for instance to invoke a `@CallDeferred` decorated super method.
    """

    def _decorated_task(self) -> tuple:
        """Build a freshly decorated task recording its calls.

        :return: Tuple of the tasks module, the decorated function and the list of calls.
        """
        from viur.core import tasks

        calls = []

        @tasks.CallDeferred
        def record_call(value):
            calls.append(value)

        self.addCleanup(tasks._deferred_tasks.pop, f"record_call.{__name__}", None)
        return tasks, record_call, calls

    def test_direct_call_without_queue(self):
        tasks, record_call, calls = self._decorated_task()
        with mock.patch.object(tasks, "queueRegion", None):
            record_call("a", _call_deferred=False)
        self.assertEqual(["a"], calls)

    def test_direct_call_without_queue_but_inside_a_request(self):
        """A request in the context must not turn the direct call into a pending task."""
        from viur.core import current

        tasks, record_call, calls = self._decorated_task()
        request = mock.MagicMock()
        request.request.headers = {}
        token = current.request.set(request)
        self.addCleanup(current.request.reset, token)

        with mock.patch.object(tasks, "queueRegion", None):
            record_call("b", _call_deferred=False)

        self.assertEqual(["b"], calls)
        request.pendingTasks.append.assert_not_called()


class TestRetryNTimesNotification(ViURTestCase):
    """The "task failed permanently" mail must actually be sent.

    :func:`viur.core.email.send_email` accepts ``tpl`` xor ``stringTemplate``. Passing
    both raises a ValueError which the surrounding ``except Exception`` swallows, so the
    notification was lost in exactly the situation it exists for.
    """

    def _run_failing_task(self, **decorator_kwargs) -> mock.Mock:
        """Run a task that always fails until the retry limit is reached.

        :return: The mock that replaced ``email.send_email``.
        """
        from viur.core import current, tasks
        from viur.core import email

        request = mock.MagicMock()
        request.request.headers = {"X-Appengine-Taskretrycount": "3"}
        token = current.request.set(request)
        self.addCleanup(current.request.reset, token)

        @tasks.retry_n_times(retries=3, email_recipients="admin@example.com", **decorator_kwargs)
        def always_fails():
            raise ValueError("boom")

        with mock.patch.object(email, "send_email") as send_email:
            with self.assertRaises(tasks.PermanentTaskFailure):
                always_fails()

        return send_email

    def test_default_template_is_passed_as_string_template(self):
        send_email = self._run_failing_task()
        send_email.assert_called_once()
        kwargs = send_email.call_args.kwargs
        self.assertNotIn("tpl", kwargs)
        self.assertIn("{{func_name}}", kwargs["stringTemplate"])

    def test_custom_template_is_passed_alone(self):
        send_email = self._run_failing_task(tpl="task_failed")
        send_email.assert_called_once()
        kwargs = send_email.call_args.kwargs
        self.assertEqual("task_failed", kwargs["tpl"])
        self.assertNotIn("stringTemplate", kwargs)

    def test_notification_reports_the_task(self):
        send_email = self._run_failing_task()
        kwargs = send_email.call_args.kwargs
        self.assertEqual("admin@example.com", kwargs["dests"])
        self.assertEqual("always_fails", kwargs["func_name"])
        self.assertEqual(3, kwargs["retries"])


class TestApplyIndexesDeferred(ViURTestCase):
    """The deferred index build has to end: within the task deadline, and without endless retries or follow-ups.

    An exception leaving a deferred task becomes a 500, and the task queue retries it - without
    ``retry_parameters`` in ``queue.yaml`` without limit. A run that outlasts the 10-minute deadline of an App
    Engine task is retried the same way.
    """

    SPEC_A = (("a", 1), ("_id", 1))
    SPEC_B = (("b", 1), ("_id", 1))
    SPEC_C = (("c", 1), ("_id", 1))

    def setUp(self):
        super().setUp()
        self.clock = [1000.0]

    def _chain(self, declared, existing, create_index, kind=None, max_runs=10) -> tuple[int, mock.Mock, mock.Mock]:
        """Run the task and every follow-up it queues, with a JSON round trip of the payload in between.

        :return: The number of runs, the ``existing`` mock and the ``create_index`` mock.
        """
        from viur.core import tasks, utils
        from viur.core.db import indexes, transport

        run = tasks.apply_indexes_deferred.__wrapped__
        collection = mock.MagicMock()
        collection.create_index.side_effect = create_index
        kwargs, runs = {}, 0
        with (
            mock.patch.object(indexes, "declared", return_value=declared),
            mock.patch.object(indexes, "existing", side_effect=existing) as existing_mock,
            mock.patch.object(transport, "get_collection", return_value=collection),
            mock.patch.object(indexes.time, "monotonic", side_effect=lambda: self.clock[0]),
            mock.patch.object(tasks, "apply_indexes_deferred") as follow_up,
        ):
            while runs < max_runs:
                runs += 1
                follow_up.reset_mock()
                run(kind, **kwargs)
                if not follow_up.called:
                    break
                kwargs = utils.json.loads(utils.json.dumps(follow_up.call_args.kwargs))
        return runs, existing_mock, collection.create_index

    def _takes(self, seconds: float, result=None):
        """A database call that advances the fake clock."""
        def call(*args):
            self.clock[0] += seconds
            if isinstance(result, Exception):
                raise result
            return result
        return call

    def test_a_run_stays_within_the_task_deadline(self):
        """Listing for 299 s and then building for 400 s ran 699 s; the driver must end it in time."""
        import pymongo
        from pymongo import _csot

        def create_index(keys):
            # Honour the deadline pymongo.timeout() sets, as the driver does for every operation.
            if (remaining := _csot.remaining()) is not None and remaining < 400:
                self.clock[0] += max(remaining, 0)
                raise pymongo.errors.ExecutionTimeout("operation exceeded time limit")
            self.clock[0] += 400
            return "a_1__id_1"

        self._chain({"thing": [self.SPEC_A]}, self._takes(299, []), create_index, max_runs=1)
        self.assertLess(self.clock[0] - 1000, 600)

    def test_the_rest_goes_to_a_follow_up_without_listing_again(self):
        """A budget used up by the inventory is no reason to stop; the next run continues where it stopped."""
        runs, existing, create_index = self._chain(
            {"a": [self.SPEC_A], "b": [self.SPEC_B]}, self._takes(400, []), self._takes(10, "x"))
        self.assertEqual(3, runs)
        self.assertEqual([mock.call("a"), mock.call("b")], existing.call_args_list)
        self.assertEqual([mock.call(list(self.SPEC_A)), mock.call(list(self.SPEC_B))], create_index.call_args_list)

    def test_a_broken_kind_does_not_stop_the_work_on_another(self):
        def existing(kind):
            if kind == "broken":
                raise ConnectionError("listIndexes failed")
            return []

        runs, existing_mock, create_index = self._chain(
            {"healthy": [self.SPEC_A, self.SPEC_B], "broken": [self.SPEC_C]}, existing, self._takes(400, "x"),
            kind="healthy",
        )
        self.assertEqual(2, runs)
        self.assertNotIn(mock.call("broken"), existing_mock.call_args_list)
        self.assertEqual([mock.call(list(self.SPEC_A)), mock.call(list(self.SPEC_B))], create_index.call_args_list)

    def test_the_chain_ends_when_nothing_can_be_created(self):
        """Without the permission to create indexes every index fails once, is logged, and the chain ends."""
        import pymongo

        with self.assertLogs(level="ERROR") as logs:
            runs, _, create_index = self._chain(
                {"a": [self.SPEC_A, self.SPEC_B], "b": [self.SPEC_C]}, self._takes(0, []),
                self._takes(200, pymongo.errors.OperationFailure("not authorized")))
        self.assertLessEqual(runs, 5)  # at most one run per unit of work: two listings, three indexes
        self.assertEqual(3, create_index.call_count)
        self.assertEqual(3, sum("not created" in line for line in logs.output))

    def test_an_unexpected_failure_ends_the_task_with_its_traceback(self):
        from viur.core import tasks
        from viur.core.db import indexes

        with (
            mock.patch.object(indexes, "declared", side_effect=ValueError("index.yaml: unknown direction 'up'")),
            self.assertLogs(level="ERROR") as logs,
            self.assertRaises(tasks.PermanentTaskFailure),
        ):
            tasks.apply_indexes_deferred.__wrapped__("thing")
        record = next(r for r in logs.records if "thing" in r.getMessage())
        self.assertIsNotNone(record.exc_info)
        self.assertIn("unknown direction", "\n".join(logs.output))

    def test_the_startup_task_hands_the_build_to_its_own_task(self):
        """Built inline, the index work would eat into the deadline of the other startup tasks of that request."""
        from viur.core import tasks
        from viur.core.db import indexes

        with (
            mock.patch.object(indexes, "missing", return_value={"thing": [self.SPEC_A]}),
            mock.patch.object(indexes, "apply") as apply,
            mock.patch.object(tasks, "apply_indexes_deferred") as build,
        ):
            tasks.create_missing_indexes()
        apply.assert_not_called()
        build.assert_called_once_with()


class TestIndexesApply(ViURTestCase):
    """``db.indexes.apply`` builds what it can and keeps to its time budget."""

    SPEC_A = (("a", 1), ("_id", 1))
    SPEC_B = (("b", 1), ("_id", 1))

    def test_a_kind_whose_indexes_cannot_be_listed_does_not_stop_the_others(self):
        from viur.core.db import indexes, transport

        def existing(kind):
            if kind == "broken":
                raise ConnectionError("listIndexes failed")
            return []

        collection = mock.MagicMock()
        collection.create_index.return_value = "b_1__id_1"
        with (
            mock.patch.object(indexes, "declared", return_value={"broken": [self.SPEC_A], "fine": [self.SPEC_B]}),
            mock.patch.object(indexes, "existing", side_effect=existing),
            mock.patch.object(transport, "get_collection", return_value=collection),
            self.assertLogs(level="ERROR") as logs,
        ):
            done = indexes.apply()

        self.assertEqual([("fine", "b_1__id_1")], [(kind, name) for kind, name, _ in done])
        collection.create_index.assert_called_once_with(list(self.SPEC_B))
        self.assertIn("broken", "\n".join(logs.output))

    def test_the_time_budget_covers_the_inventory(self):
        from viur.core.db import indexes, transport

        clock = [1000.0]

        def slow_existing(kind):
            clock[0] += 10  # listing the indexes of a kind took 10 s
            return []

        collection = mock.MagicMock()
        with (
            mock.patch.object(indexes, "declared", return_value={"thing": [self.SPEC_A]}),
            mock.patch.object(indexes, "existing", side_effect=slow_existing),
            mock.patch.object(indexes.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(transport, "get_collection", return_value=collection),
        ):
            done = indexes.apply(time_budget=5)

        self.assertEqual([], done)
        collection.create_index.assert_not_called()

    def test_the_time_budget_stops_listing_further_kinds(self):
        """With an unreachable database every kind waits for the server selection before it fails."""
        from viur.core.db import indexes

        clock = [1000.0]

        def unreachable(kind):
            clock[0] += 30
            raise ConnectionError("server selection timed out")

        with (
            mock.patch.object(indexes, "declared", return_value={"a": [self.SPEC_A], "b": [self.SPEC_B]}),
            mock.patch.object(indexes, "existing", side_effect=unreachable) as existing,
            mock.patch.object(indexes.time, "monotonic", side_effect=lambda: clock[0]),
            self.assertLogs(level="ERROR"),
        ):
            self.assertEqual([], indexes.apply(time_budget=5))
        existing.assert_called_once_with("a")
