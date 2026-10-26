"""
Tests for GoogleAccount.login:
  An existing user is matched by the Google user ID ("sub") first and by e-mail only when Google
  verified the address; no account is entered or created with an unverified e-mail; the
  gsuite domain rule relies on the "hd" claim, not on the e-mail suffix.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from abstract import ViURTestCase  # noqa: E402


class FakeSkel(dict):
    """Stands in for the user skeleton and its query; only what GoogleAccount.login touches."""

    def __init__(self, users: list[dict]):
        super().__init__(sync=True)
        self.users = users
        self.filters = []
        self.match = None
        self.written = False

    def __missing__(self, key):
        return None

    def all(self):
        return self

    def filter(self, condition: str, value: str):
        self.filters.append(condition)
        field = {"uid =": "uid", "name.idx =": "name"}[condition]
        self.match = next((user for user in self.users if (user.get(field) or "").lower() == value), None)
        return self

    def getSkel(self):
        return self.match

    def write(self):
        self.written = True
        self["key"] = "new-user"
        return self


def _token(**claims) -> dict:
    return {
        "iss": "https://accounts.google.com",
        "sub": "google-sub-1",
        "email": "jane@example.com",
        "email_verified": True,
    } | claims


class TestGoogleLogin(ViURTestCase):

    def setUp(self):
        super().setUp()
        from viur.core import conf
        from viur.core.modules.user import GoogleAccount

        self._conf_backup = conf.user.google_client_id, conf.user.google_gsuite_domains
        conf.user.google_client_id = "client-id"
        conf.user.google_gsuite_domains = []

        self.provider = object.__new__(GoogleAccount)
        self.provider._user_module = mock.MagicMock()

    def tearDown(self):
        from viur.core import conf
        conf.user.google_client_id, conf.user.google_gsuite_domains = self._conf_backup
        super().tearDown()

    def _login(self, user_info: dict, users: list[dict]) -> None:
        self.skel = FakeSkel(users)
        self.provider._user_module.baseSkel.return_value = self.skel
        with mock.patch("google.oauth2.id_token.verify_oauth2_token", return_value=user_info):
            self.provider.login._func(self.provider, token="token")

    def _logged_in_key(self) -> str:
        self.provider._user_module.continueAuthenticationFlow.assert_called_once()
        return self.provider._user_module.continueAuthenticationFlow.call_args.args[1]

    def test_unverified_email_does_not_enter_existing_account(self):
        from viur.core import errors
        unverified = _token(email_verified=False), _token(email_verified="false"), _token()
        del unverified[2]["email_verified"]
        for user_info in unverified:
            with self.subTest(email_verified=user_info.get("email_verified", "missing")):
                with self.assertRaises(errors.Forbidden):
                    self._login(user_info, [{"key": "victim", "name": "jane@example.com", "sync": False}])
                self.provider._user_module.continueAuthenticationFlow.assert_not_called()

    def test_unverified_email_does_not_create_account(self):
        from viur.core import errors
        from viur.core.modules.user import GoogleAccount
        with mock.patch.object(GoogleAccount, "registrationEnabled", True):
            with self.assertRaises(errors.Forbidden):
                self._login(_token(email_verified=False), [])
        self.assertFalse(self.skel.written)

    def test_verified_email_matches_existing_user(self):
        self._login(_token(), [{"key": "jane", "name": "Jane@Example.com", "sync": False}])
        self.assertEqual(self._logged_in_key(), "jane")
        self.assertEqual(self.skel.filters, ["uid =", "name.idx ="])

    def test_existing_user_is_matched_by_uid_first(self):
        self._login(
            _token(email="jane.new@example.com"),
            [
                {"key": "other", "name": "jane.new@example.com", "sync": False},
                {"key": "jane", "name": "jane@example.com", "uid": "google-sub-1", "sync": False},
            ],
        )
        self.assertEqual(self._logged_in_key(), "jane")
        self.assertEqual(self.skel.filters, ["uid ="])

    def test_gsuite_domain_creates_user_with_hd_claim(self):
        from viur.core import conf
        conf.user.google_gsuite_domains = ["example.com"]
        self._login(_token(hd="example.com"), [])
        self.assertTrue(self.skel.written)
        self.assertEqual((self.skel["uid"], self.skel["name"]), ("google-sub-1", "jane@example.com"))
        self.assertEqual(self._logged_in_key(), "new-user")

    def test_gsuite_domain_needs_hd_claim_not_email_suffix(self):
        from viur.core import conf, errors
        conf.user.google_gsuite_domains = ["example.com"]
        with self.assertRaises(errors.Forbidden):
            self._login(_token(), [])  # the e-mail ends with @example.com, but there is no "hd"
        self.assertFalse(self.skel.written)
