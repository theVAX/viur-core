import logging
from unittest import mock

from abstract import ViURTestCase


class TestLoggingDefaults(ViURTestCase):

    def test_pymongo_does_not_log_every_command(self):
        import viur.core.logging  # noqa: F401 -- configures the loggers on import

        self.assertGreaterEqual(logging.getLogger("pymongo").getEffectiveLevel(), logging.WARNING)
        for name in ("pymongo.command", "pymongo.serverSelection", "pymongo.connection"):
            with self.subTest(logger=name):
                self.assertFalse(logging.getLogger(name).isEnabledFor(logging.DEBUG))

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
