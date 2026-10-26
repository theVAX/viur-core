"""
Tests for TimeBasedOTP:
  updateTimeDrift writes the clamped drift onto the user,
  otp() with a drifted token completes the login and records the drift.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from abstract import ViURTestCase  # noqa: E402


class FakeSession(dict):
    def markChanged(self):
        pass


class TestTimeBasedOTPTimeDrift(ViURTestCase):

    def setUp(self):
        super().setUp()
        from viur.core.modules.user import TimeBasedOTP

        self.provider = object.__new__(TimeBasedOTP)
        self.provider._user_module = mock.MagicMock()
        self.user_skel = self.provider._user_module.skel.return_value.read.return_value

    def test_update_time_drift_patches_user(self):
        for idx, change in ((5, 0.3), (-5, -0.3), (2, 0.2)):
            with self.subTest(idx=idx):
                self.user_skel.reset_mock()
                self.provider.updateTimeDrift("user-key", idx)
                self.provider._user_module.skel.return_value.read.assert_called_with("user-key")
                self.user_skel.patch.assert_called_once_with({"+otp_timedrift": change}, update_relations=False)

    def test_update_time_drift_ignores_missing_user(self):
        self.provider._user_module.skel.return_value.read.return_value = None
        self.provider.updateTimeDrift("gone", 5)  # must not raise

    def test_otp_with_drifted_token_logs_in(self):
        from viur.core import current
        from viur.core.modules.user import TimeBasedOTP

        current.session.set(FakeSession(_otp_user={"key": "user-key", "secret": "00", "timedrift": 0.0}))
        with mock.patch.object(TimeBasedOTP, "verify", return_value=4):
            self.provider.otp._func(self.provider, otptoken="123456")

        self.user_skel.patch.assert_called_once_with({"+otp_timedrift": 0.3}, update_relations=False)
        self.provider._user_module.secondFactorSucceeded.assert_called_once_with(self.provider, "user-key")
        self.assertNotIn("_otp_user", current.session.get())
