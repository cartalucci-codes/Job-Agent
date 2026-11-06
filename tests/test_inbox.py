"""Gmail connect + inbox tracking, with Google replaced by fakes."""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("JOBAGENT_DATA", tempfile.mkdtemp(prefix="jobagent-inbox-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobagent import config, db, gmail, tracker  # noqa: E402
from jobagent.main import inbox_message  # noqa: E402

db.init()
(config.DATA_DIR / "gmail_client.json").write_text(json.dumps(
    {"installed": {"client_id": "id", "client_secret": "secret"}}))


class FakePost:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, json.dumps(payload)

    def json(self):
        return self._p


def ms(days_ago=0.0):
    return int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).timestamp() * 1000)


class ClientFileTests(unittest.TestCase):
    GOOD = {"installed": {"client_id": "abc.apps.googleusercontent.com", "client_secret": "s3cret"}}

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jobagent-client-"))
        self._orig = gmail.DATA_DIR
        gmail.DATA_DIR = self.dir

    def tearDown(self):
        gmail.DATA_DIR = self._orig

    def put(self, name, content, encoding="utf-8"):
        (self.dir / name).write_bytes(content.encode(encoding) if isinstance(content, str) else content)

    def test_missing_file_names_the_folder_and_lists_what_is_there(self):
        self.put("config.yaml", "x: 1")
        problem = gmail.client_problem()
        self.assertIn(str(self.dir), problem)
        self.assertIn("config.yaml", problem)
        self.assertFalse(gmail.is_configured())

    def test_exact_name_works(self):
        self.put("gmail_client.json", json.dumps(self.GOOD))
        self.assertIsNone(gmail.client_problem())
        self.assertEqual(gmail._client()["client_id"], "abc.apps.googleusercontent.com")

    def test_googles_download_name_works(self):
        self.put("client_secret_123-abc.apps.googleusercontent.com.json", json.dumps(self.GOOD))
        self.assertTrue(gmail.is_configured())

    def test_double_extension_from_hidden_windows_extensions_works(self):
        self.put("gmail_client.json.json", json.dumps(self.GOOD))
        self.assertTrue(gmail.is_configured())

    def test_web_client_type_works(self):
        self.put("gmail_client.json", json.dumps({"web": self.GOOD["installed"]}))
        self.assertTrue(gmail.is_configured())

    def test_byte_order_mark_is_tolerated(self):
        self.put("gmail_client.json", b"\xef\xbb\xbf" + json.dumps(self.GOOD).encode())
        self.assertTrue(gmail.is_configured())

    def test_bad_json(self):
        self.put("gmail_client.json", "not json at all")
        self.assertIn("could not be read as JSON", gmail.client_problem())

    def test_service_account_key_is_recognised(self):
        self.put("gmail_client.json", json.dumps({"type": "service_account", "private_key": "x"}))
        self.assertIn("service-account", gmail.client_problem())

    def test_missing_secret(self):
        self.put("gmail_client.json", json.dumps({"installed": {"client_id": "abc"}}))
        self.assertIn("client_secret", gmail.client_problem())

    def test_auth_url_without_client_explains(self):
        with self.assertRaises(gmail.GmailError) as ctx:
            gmail.auth_url("http://127.0.0.1:8787/gmail/callback", "state", False)
        self.assertIn(str(self.dir), str(ctx.exception))


class ConnectTests(unittest.TestCase):
    def setUp(self):
        self._post, self._api = gmail.requests.post, gmail.api
        gmail.disconnect()

    def tearDown(self):
        gmail.requests.post, gmail.api = self._post, self._api
        gmail.disconnect()

    def test_unticked_permission_is_reported_and_nothing_is_saved(self):
        gmail.requests.post = lambda *a, **k: FakePost({"access_token": "a", "refresh_token": "r", "scope": "openid", "expires_in": 3600})
        with self.assertRaises(gmail.GmailError) as ctx:
            gmail.exchange_code("code", "http://127.0.0.1:8787/gmail/callback")
        self.assertIn("tick the box", str(ctx.exception))
        self.assertFalse(gmail.is_connected())

    def test_gmail_api_not_answering_is_reported_and_connection_cleared(self):
        gmail.requests.post = lambda *a, **k: FakePost({"access_token": "a", "refresh_token": "r", "scope": gmail.READONLY, "expires_in": 3600})

        def boom(*a, **k):
            raise gmail.GmailError("Gmail API 403: accessNotConfigured")
        gmail.api = boom
        with self.assertRaises(gmail.GmailError) as ctx:
            gmail.exchange_code("code", "http://127.0.0.1:8787/gmail/callback")
        self.assertIn("SAME", str(ctx.exception))
        self.assertIn("accessNotConfigured", str(ctx.exception))
        self.assertFalse(gmail.is_connected())

    def test_good_connection(self):
        gmail.requests.post = lambda *a, **k: FakePost({"access_token": "a", "refresh_token": "r", "scope": gmail.READONLY + " openid", "expires_in": 3600})
        gmail.api = lambda method, path, **k: {"emailAddress": "me@example.com"}
        gmail.exchange_code("code", "http://127.0.0.1:8787/gmail/callback")
        self.assertTrue(gmail.is_connected())
        self.assertEqual(gmail.account_email(), "me@example.com")
        self.assertFalse(gmail.can_send())

    def test_refresh_token_is_stored_encrypted(self):
        gmail.requests.post = lambda *a, **k: FakePost({"access_token": "a", "refresh_token": "plain-refresh-xyz", "scope": gmail.READONLY, "expires_in": 3600})
        gmail.api = lambda method, path, **k: {"emailAddress": "me@example.com"}
        gmail.exchange_code("code", "x")
        self.assertNotIn("plain-refresh-xyz", db.kv_get("gmail_refresh"))


class PollTests(unittest.TestCase):
    def setUp(self):
        self._saved = (gmail.is_connected, gmail.list_message_ids, gmail.get_message_meta)
        gmail.is_connected = lambda: True
        self.messages = {}
        gmail.list_message_ids = lambda q, max_pages=3: list(self.messages)
        gmail.get_message_meta = lambda i: self.messages[i]
        tracker.reset_seen()

    def tearDown(self):
        gmail.is_connected, gmail.list_message_ids, gmail.get_message_meta = self._saved

    def add_app(self, company, title, status="applied", days=4):
        jid = db.execute("INSERT INTO jobs (source, external_id, title, company, found_at, status) VALUES ('t', ?, ?, ?, ?, 'matched')",
                         (f"{company}{title}", title, company, db.now()))
        applied = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return db.execute("INSERT INTO applications (job_id, status, created_at, applied_at) VALUES (?,?,?,?)", (jid, status, db.now(), applied))

    def msg(self, mid, sender, subject, snippet="", days_ago=1.0):
        self.messages[mid] = {"id": mid, "thread_id": "t" + mid, "from": sender, "subject": subject, "snippet": snippet,
                              "internal_ms": ms(days_ago)}

    def status(self, app_id):
        return db.one("SELECT status FROM applications WHERE id=?", (app_id,))["status"]

    def test_no_applications_is_reported_clearly_and_counts_as_checked(self):
        saved = db.query("SELECT id, status FROM applications")
        db.execute("UPDATE applications SET status='dismissed'")
        try:
            db.kv_set("last_inbox_check", None)
            result = tracker.poll_inbox()
            self.assertEqual(result, {"tracking": 0, "checked": 0, "matched": 0})
            self.assertIsNotNone(db.kv_get("last_inbox_check"))  # dashboard no longer shows "checked -"
            self.assertIn("no applications to track", inbox_message(result))
        finally:
            for row in saved:
                db.execute("UPDATE applications SET status=? WHERE id=?", (row["status"], row["id"]))

    def test_matching_and_progression(self):
        app = self.add_app("Zephyrine Labs", "Graduate Developer")
        self.msg("m1", "Greenhouse <no-reply@greenhouse-mail.io>", "Thanks for applying to Zephyrine Labs", "We received your application.", 3)
        self.msg("m2", "Thandi at Zephyrine Labs <talent@zephyrine.example>", "Interview invitation", "We would like to schedule a call.", 1)
        self.msg("m3", "Newsletter <hi@news.example.com>", "Weekly digest", "Nothing relevant")
        result = tracker.poll_inbox()
        self.assertEqual((result["checked"], result["matched"]), (3, 2))
        self.assertEqual(self.status(app), "interviewing")
        kinds = [e["kind"] for e in db.query("SELECT kind FROM events WHERE application_id=? ORDER BY at", (app,))]
        self.assertIn("ack", kinds)
        self.assertIn("interview_invite", kinds)
        self.assertIsNotNone(db.one("SELECT first_response_at FROM applications WHERE id=?", (app,))["first_response_at"])
        self.assertIn("matched", inbox_message(result))
        # a second check finds nothing new
        self.assertEqual(tracker.poll_inbox()["checked"], 0)
        # a full rescan re-reads everything but does not duplicate events
        before = db.one("SELECT COUNT(*) n FROM events WHERE application_id=?", (app,))["n"]
        self.assertEqual(tracker.poll_inbox(full=True)["checked"], 3)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM events WHERE application_id=?", (app,))["n"], before)

    def test_application_added_after_the_email_arrived_is_still_matched(self):
        self.msg("late1", "Recruiter <jobs@quillcorp.example>", "Your application to Quillcorp", "We have decided to move forward with other candidates.", 2)
        tracker.poll_inbox()  # email is seen while no application matches it
        app = self.add_app("Quillcorp", "Developer")
        tracker.reset_seen()  # what pressing "I applied" does
        tracker.poll_inbox()
        self.assertEqual(self.status(app), "rejected")

    def test_failure_is_surfaced_then_cleared(self):
        self.add_app("Errco", "Dev")

        def boom(q, max_pages=3):
            raise gmail.GmailError("Gmail API 401: invalid credentials")
        gmail.list_message_ids = boom
        result = tracker.poll_inbox()
        self.assertIn("error", result)
        self.assertIn("401", db.kv_get("last_inbox_error"))
        self.assertTrue(inbox_message(result).startswith("Gmail problem"))
        gmail.list_message_ids = lambda q, max_pages=3: []
        tracker.poll_inbox()
        self.assertIsNone(db.kv_get("last_inbox_error"))

    def test_not_connected(self):
        gmail.is_connected = lambda: False
        self.assertEqual(tracker.poll_inbox(), {"skipped": "Gmail is not connected"})


if __name__ == "__main__":
    unittest.main()
