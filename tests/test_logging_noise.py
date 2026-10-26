import json
import logging
import subprocess
import sys
import textwrap
from unittest import mock

from abstract import ViURTestCase


class TestLoggingDefaults(ViURTestCase):

    def test_pymongo_does_not_log_every_command(self):
        import viur.core.logging  # noqa: F401 -- configures the loggers on import

        self.assertGreaterEqual(logging.getLogger("pymongo").getEffectiveLevel(), logging.WARNING)
        for name in ("pymongo.command", "pymongo.serverSelection", "pymongo.connection"):
            with self.subTest(logger=name):
                self.assertFalse(logging.getLogger(name).isEnabledFor(logging.DEBUG))

    def test_pymongo_can_be_switched_back_on(self):
        import viur.core.logging  # noqa: F401 -- configures the loggers on import

        pymongo_logger = logging.getLogger("pymongo")
        self.addCleanup(pymongo_logger.setLevel, pymongo_logger.level)
        pymongo_logger.setLevel(logging.DEBUG)

        self.assertTrue(logging.getLogger("pymongo.command").isEnabledFor(logging.DEBUG))

    def test_explicit_pymongo_levels_are_kept(self):
        """Levels an app sets before importing viur.core stay as they are."""
        script = textwrap.dedent("""
            import json, logging
            from unittest import mock
            import google.auth
            import requests
            google.auth.default = mock.Mock(return_value=(mock.Mock(), "test-project"))
            logging.getLogger("pymongo").setLevel(logging.INFO)
            logging.getLogger("pymongo.command").setLevel(logging.DEBUG)
            not_found = requests.Response()
            not_found.status_code = 404
            with mock.patch("requests.get", return_value=not_found):
                import viur.core.logging
            print(json.dumps([logging.getLogger(n).level for n in ("pymongo", "pymongo.command")]))
        """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([logging.INFO, logging.DEBUG], json.loads(result.stdout.splitlines()[-1]))

    def test_request_logger_does_not_detect_its_resource(self):
        """Without a resource, google-cloud-logging asks the metadata server what it runs on."""
        import importlib
        # not `from viur.core import logging`: the package attribute is the standard library's logging
        viur_logging = importlib.import_module("viur.core.logging")

        self.assertIs(viur_logging.requestLoggingRessource, viur_logging.requestLogger.default_resource)


class TestTransactionConflictLogging(ViURTestCase):

    def setUp(self) -> None:
        super().setUp()
        from viur.core.config import conf
        from viur.core.db import transport
        for patch in (
            mock.patch.object(transport, "_mongo"),
            mock.patch.object(transport.time, "sleep"),
            mock.patch.object(conf.db, "transaction_attempts", 2),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_retried_conflict_is_a_warning(self):
        import pymongo.errors
        from viur.core.db import transport

        func = mock.Mock(side_effect=[
            pymongo.errors.PyMongoError("conflict", error_labels=["TransientTransactionError"]),
            "done",
        ])

        with self.assertLogs(transport.logger, logging.WARNING) as logs:
            self.assertEqual("done", transport.run_in_transaction(func))

        self.assertEqual(["WARNING"], [record.levelname for record in logs.records])

    def test_exhausted_retries_raise_without_an_error_log(self):
        """The final failure is reported by the RuntimeError; whoever handles it decides how to log it."""
        import pymongo.errors
        from viur.core.db import transport

        func = mock.Mock(side_effect=pymongo.errors.PyMongoError("conflict", error_labels=["TransientTransactionError"]))

        with self.assertLogs(transport.logger, logging.WARNING) as logs:
            with self.assertRaisesRegex(RuntimeError, "Maximum transaction retries exceeded"):
                transport.run_in_transaction(func)

        self.assertEqual(2, func.call_count)
        self.assertEqual(["WARNING"], [record.levelname for record in logs.records])
