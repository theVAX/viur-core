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
    """The deferred index build must end, not be retried by the task queue forever.

    An exception leaving a deferred task becomes a 500, and the task queue retries it - without
    ``retry_parameters`` in ``queue.yaml`` without limit.
    """

    def test_a_failure_ends_the_task(self):
        from viur.core import tasks
        from viur.core.db import indexes

        with (
            mock.patch.object(indexes, "apply", side_effect=ConnectionError("listIndexes failed")),
            self.assertLogs(level="ERROR") as logs,
            self.assertRaises(tasks.PermanentTaskFailure),
        ):
            tasks.apply_indexes_deferred.__wrapped__()
        self.assertIn("listIndexes failed", "\n".join(logs.output))

    def test_the_run_has_a_time_budget(self):
        from viur.core import tasks
        from viur.core.db import indexes

        with (
            mock.patch.object(indexes, "apply", return_value=[]) as apply,
            mock.patch.object(indexes, "missing", return_value={}),
        ):
            tasks.apply_indexes_deferred.__wrapped__("thing")
        apply.assert_called_once_with("thing", time_budget=tasks._INDEX_TIME_BUDGET)

    def test_queues_itself_again_while_it_makes_progress(self):
        from viur.core import tasks
        from viur.core.db import indexes

        run = tasks.apply_indexes_deferred.__wrapped__
        with (
            mock.patch.object(indexes, "apply", return_value=[("thing", "a_1__id_1", 75.0)]),
            mock.patch.object(indexes, "missing", return_value={"thing": [(("b", 1), ("_id", 1))]}),
            mock.patch.object(tasks, "apply_indexes_deferred") as requeue,
        ):
            run()
        requeue.assert_called_once_with(None)

    def test_an_index_created_but_still_missing_ends_the_chain(self):
        """``createIndex`` succeeds for an index that exists already; if the listing never shows it, it stays missing."""
        from viur.core import tasks
        from viur.core.db import indexes

        run = tasks.apply_indexes_deferred.__wrapped__
        with (
            mock.patch.object(indexes, "apply", return_value=[("thing", "b_1__id_1", 0.1)]),
            mock.patch.object(indexes, "missing", return_value={"thing": [(("b", 1), ("_id", 1))]}),
            mock.patch.object(tasks, "apply_indexes_deferred") as requeue,
        ):
            run()
        requeue.assert_not_called()

    def test_a_run_without_progress_ends_the_chain(self):
        """Without the permission to create indexes every run would fail the same way."""
        from viur.core import tasks
        from viur.core.db import indexes

        run = tasks.apply_indexes_deferred.__wrapped__
        with (
            mock.patch.object(indexes, "apply", return_value=[]),
            mock.patch.object(indexes, "missing", return_value={"thing": [(("b", 1), ("_id", 1))]}),
            mock.patch.object(tasks, "apply_indexes_deferred") as requeue,
        ):
            run()
        requeue.assert_not_called()


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
