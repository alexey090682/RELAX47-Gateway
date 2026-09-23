import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from site_accounts import SiteAccountStore


class SiteAccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SiteAccountStore(Path(self.temp.name) / "site.db")
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_registration_login_session_and_logout(self):
        payload = {"name": "Марина", "email": "MARINA@example.com", "password": "надёжный-пароль-123", "consent": "on"}
        user, token = self.store.register(payload)
        self.assertEqual(user["email"], "marina@example.com")
        self.assertEqual(self.store.session_user(token)["id"], user["id"])
        logged_in, second = self.store.login({"email": user["email"], "password": payload["password"]})
        self.assertEqual(logged_in["id"], user["id"])
        self.store.logout(second)
        self.assertIsNone(self.store.session_user(second))

    def test_password_is_hashed_and_email_is_unique(self):
        password = "long-password-which-is-safe"
        self.store.register({"name": "Гость", "email": "guest@example.com", "password": password, "consent": True})
        with sqlite3.connect(self.store.path) as connection:
            salt, digest = connection.execute("SELECT password_salt,password_hash FROM website_users").fetchone()
        self.assertEqual(len(salt), 16)
        self.assertEqual(len(digest), 32)
        self.assertNotEqual(digest, password.encode())
        with self.assertRaisesRegex(ValueError, "уже существует"):
            self.store.register({"name": "Другой", "email": "GUEST@example.com", "password": "another-long-password", "consent": True})

    def test_saved_quotes_are_validated_and_deduplicated(self):
        user, _ = self.store.register({"name": "Гость", "email": "guest@example.com", "password": "secure-password-123", "consent": True})
        arrival = date.today() + timedelta(days=1)
        quote = {"arrival": arrival.isoformat(), "departure": (arrival + timedelta(days=2)).isoformat(), "guests": 4, "rooms": 2, "spa": "heated", "spaDays": 1, "spaExtra": 0, "early": 0, "late": 0}
        first = self.store.save_quote(user["id"], quote)
        second = self.store.save_quote(user["id"], quote)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.list_quotes(user["id"])), 1)


if __name__ == "__main__":
    unittest.main()
