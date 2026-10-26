"""
Tests for GoogleAccount.login:
  An existing user is matched by the Google user ID ("sub") first and by e-mail only when Google
  verified the address; no account is entered or created with an unverified e-mail; the first
  match by e-mail binds the user to that Google account, in the same transaction that checks no
  other account is bound; the gsuite domain rule relies on the "hd" claim, not on the e-mail suffix.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from abstract import ViURTestCase  # noqa: E402


class FakeRecord(dict):
    """A user as a skeleton returns it: unset bones read as None."""

    def __missing__(self, key):
        return None


class FakeUser(FakeRecord):
    """A user found by a query, read from its stored entry."""

    def __init__(self, stored: dict):
        super().__init__(stored)
        self.stored = stored

    def patch(self, values):
        """Like Skeleton.patch: read the stored entry again, change it and write it back in one transaction."""
        self.clear()
        self.update(self.stored)
        values(self)
        self.stored.update(self)
        return self


class FakeSkel(FakeRecord):
    """Stands in for the user skeleton and its query; only what GoogleAccount.login touches."""

    def __init__(self, users: list[dict], written_meanwhile: dict | None = None):
        super().__init__(sync=True)
        self.users = users  # the stored entries; they keep the changes of a login
        self.written_meanwhile = written_meanwhile or {}
        self.filters = []
        self.match = None
        self.written = False

    def all(self):
        return self

    def filter(self, condition: str, value: str):
        self.filters.append(condition)
        field = {"uid =": "uid", "name.idx =": "name"}[condition]
        self.match = next((user for user in self.users if (user.get(field) or "").lower() == value), None)
        return self

    def getSkel(self):
        if self.match is None:
            return None
        user = FakeUser(self.match)
        # A concurrent login writes to the stored user after this lookup.
        self.match.update(self.written_meanwhile)
        return user

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

    def _login(self, user_info: dict, users: list[dict], written_meanwhile: dict | None = None) -> None:
        self.skel = FakeSkel(users, written_meanwhile)
        self.provider._user_module.baseSkel.return_value = self.skel
        with mock.patch("google.oauth2.id_token.verify_oauth2_token", return_value=user_info) as self.verify:
            self.provider.login._func(self.provider, token="token")

    def _logged_in_key(self) -> str:
        self.provider._user_module.continueAuthenticationFlow.assert_called_once()
        return self.provider._user_module.continueAuthenticationFlow.call_args.args[1]

    def _assert_refused(self, user_info: dict, users: list[dict], written_meanwhile: dict | None = None) -> None:
        """The login is refused; no user is created or logged in."""
        from viur.core import errors
        self.provider._user_module.reset_mock()
        with self.assertRaises(errors.Forbidden):
            self._login(user_info, users, written_meanwhile)
        self.assertFalse(self.skel.written)
        self.provider._user_module.continueAuthenticationFlow.assert_not_called()

    def test_unverified_email_does_not_enter_existing_account(self):
        unverified = _token(email_verified=False), _token(email_verified="false"), _token()
        del unverified[2]["email_verified"]
        for user_info in unverified:
            with self.subTest(email_verified=user_info.get("email_verified", "missing")):
                self._assert_refused(user_info, [{"key": "victim", "name": "jane@example.com", "sync": False}])
                self.provider._user_module.baseSkel.assert_not_called()

    def test_unverified_email_is_refused_before_lookup_and_sync(self):
        from viur.core import conf
        from viur.core.modules.user import GoogleAccount
        conf.user.google_gsuite_domains = ["example.com"]
        unverified = _token(email_verified=False)
        for case, user_info, users in (
            ("uid-bound user with sync", unverified, [{"key": "jane", "name": "old@example.com",
                                                      "uid": "google-sub-1", "sync": True}]),
            ("allowed hosted domain", unverified | {"hd": "example.com"}, []),
        ):
            with self.subTest(case):
                self._assert_refused(user_info, users)
                self.provider._user_module.baseSkel.assert_not_called()
        with mock.patch.object(GoogleAccount, "registrationEnabled", True), self.subTest("registration enabled"):
            self._assert_refused(unverified, [])
            self.provider._user_module.baseSkel.assert_not_called()

    def test_verified_email_matches_existing_user(self):
        self._login(_token(), [{"key": "jane", "name": "Jane@Example.com", "sync": False}])
        self.assertEqual(self._logged_in_key(), "jane")
        self.assertEqual(self.skel.filters, ["uid =", "name.idx ="])
        self.verify.assert_called_once_with("token", mock.ANY, "client-id")

    def test_email_does_not_match_user_bound_to_other_google_account(self):
        self._assert_refused(
            _token(),  # sub "google-sub-1"
            [{"key": "jane", "name": "jane@example.com", "uid": "google-sub-2", "sync": True}],
        )
        self.assertEqual(self.skel.filters, ["uid =", "name.idx ="])

    def test_first_email_match_binds_google_account(self):
        users = [{"key": "jane", "name": "jane@example.com", "sync": True}]
        self._login(_token(given_name="Jane", family_name="Doe"), users)
        self.assertEqual(self._logged_in_key(), "jane")
        self.assertEqual(
            (users[0]["uid"], users[0]["firstname"], users[0]["lastname"]),
            ("google-sub-1", "Jane", "Doe"),
        )

    def test_first_email_match_binds_google_account_without_sync(self):
        users = [{"key": "jane", "name": "Jane@Example.com", "sync": False}]
        self._login(_token(given_name="Jane"), users)
        self.assertEqual(self._logged_in_key(), "jane")
        self.assertEqual(users[0], {"key": "jane", "name": "Jane@Example.com", "sync": False, "uid": "google-sub-1"})

    def test_other_google_account_with_same_email_is_refused_after_first_login(self):
        users = [{"key": "jane", "name": "jane@example.com", "sync": True}]
        self._login(_token(), users)  # sub "google-sub-1"
        self._assert_refused(_token(sub="google-sub-2"), users)
        self.assertEqual(self.skel.filters, ["uid =", "name.idx ="])
        self.assertEqual(users[0]["uid"], "google-sub-1")

    def test_concurrent_first_login_of_other_google_account_wins(self):
        # The lookup sees no uid, but another Google account binds the user before this login writes.
        users = [{"key": "jane", "name": "jane@example.com", "sync": True}]
        self._assert_refused(_token(given_name="Mallory"), users, written_meanwhile={"uid": "google-sub-2"})
        self.assertEqual(users[0], {"key": "jane", "name": "jane@example.com", "sync": True, "uid": "google-sub-2"})

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
