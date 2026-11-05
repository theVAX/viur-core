from urllib.parse import unquote

from abstract import ViURTestCase


class TestErrorHeaderValue(ViURTestCase):
    """`x-viur-error` carries only printable ASCII, and the description can be restored from it."""

    def _value(self, descr: str) -> str:
        from viur.core.request import _error_header_value
        return _error_header_value(descr)

    def test_ascii_is_unchanged(self):
        descr = "Invalid value for 'name': must be <= 10 chars (see /docs?x=1&y=2)!"
        self.assertEqual(descr, self._value(descr))

    def test_non_latin1_is_encoded_and_restorable(self):
        descr = "Mail zu groß – höchstens 25 MB „ja“…"
        value = self._value(descr)
        self.assertEqual("Mail zu gro%C3%9F %E2%80%93 h%C3%B6chstens 25 MB %E2%80%9Eja%E2%80%9C%E2%80%A6", value)
        self.assertEqual(descr, unquote(value))

    def test_only_printable_ascii(self):
        value = self._value("a\r\nX-Injected: 1\tb\x00c\x7fdé\U0001F600")
        self.assertTrue(all(0x20 <= ord(c) < 0x7F for c in value), value)
        self.assertEqual("a%0DX-Injected: 1%09b%00c%7Fd%C3%A9%F0%9F%98%80", value)

    def test_newline_is_dropped_and_percent_is_escaped(self):
        self.assertEqual("line oneline two", self._value("line one\nline two"))
        self.assertEqual("100%25 sure", self._value("100% sure"))
        self.assertEqual("100% sure", unquote(self._value("100% sure")))
