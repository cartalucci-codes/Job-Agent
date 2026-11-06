"""Run with:  python -m unittest discover -s tests -v"""
import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Isolated data dir; must be set before the package is imported.
_TMP = tempfile.mkdtemp(prefix="jobagent-test-")
os.environ["JOBAGENT_DATA"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobagent import applier, config, crypto, db, matcher, pipeline, tracker  # noqa: E402
from jobagent.sources import Job, find_apply_email  # noqa: E402

SEARCH = config.load_config()["search"]


def job(title, desc="", location="Cape Town", remote=False):
    return {"title": title, "description": desc, "location": location, "remote": remote}


class MatcherTests(unittest.TestCase):
    def test_strong_match(self):
        score, why = matcher.score_job(job("Junior Python Developer", "We use Python, SQL and Azure with git."), SEARCH)
        self.assertGreaterEqual(score, 75, why)

    def test_seniority_excluded(self):
        score, why = matcher.score_job(job("Senior Software Engineer", "Python"), SEARCH)
        self.assertEqual(score, 0)
        self.assertIn("excluded", why[0])

    def test_no_skills_never_queued(self):
        score, _ = matcher.score_job(job("Software Engineer", "Rust and Go only"), SEARCH)
        self.assertLess(score, SEARCH["min_score"])

    def test_special_character_skills(self):
        _, why = matcher.score_job(job("Software Developer", "Experience with C# and .NET required"), SEARCH)
        self.assertTrue(any("c#" in w and ".net" in w for w in why), why)

    def test_word_boundaries(self):
        # "sql" must not match inside "mysqlish"; "lead" must not match "leader"
        self.assertEqual(matcher.matched_skills(job("Developer", "mysqlish stuff"), SEARCH), [])
        score, _ = matcher.score_job(job("Team leader assistant developer", "python"), SEARCH)
        self.assertGreater(score, 0)

    def test_remote_counts_as_location(self):
        a, _ = matcher.score_job(job("Software Engineer", "python sql", "Worldwide", remote=True), SEARCH)
        b, _ = matcher.score_job(job("Software Engineer", "python sql", "Berlin", remote=False), SEARCH)
        self.assertGreater(a, b)

    def test_remote_limited_to_other_regions_does_not_count(self):
        anywhere, _ = matcher.score_job(job("Software Engineer", "python sql", "Worldwide", remote=True), SEARCH)
        us_only, why = matcher.score_job(job("Software Engineer", "python sql", "USA Only", remote=True), SEARCH)
        self.assertEqual(anywhere - us_only, 15, why)
        self.assertTrue(any("limited to" in w for w in why), why)
        # a "Remote - US" location must not sneak in through the plain "Remote" location entry
        s2, _ = matcher.score_job(job("Software Engineer", "python sql", "Remote - US", remote=True), SEARCH)
        self.assertEqual(s2, us_only)

    def test_remote_regions_that_include_you(self):
        for place in ("Remote (Africa)", "EMEA", "Anywhere in the World", "Europe, Africa", "", "Remote"):
            self.assertTrue(matcher.remote_usable(place, SEARCH["remote_regions"]), place)
        for place in ("USA Only", "United States, Canada", "UK"):
            self.assertFalse(matcher.remote_usable(place, SEARCH["remote_regions"]), place)


class ClassifyTests(unittest.TestCase):
    def check(self, subject, snippet, expected):
        self.assertEqual(tracker.classify(subject, snippet)[0], expected, f"{subject} | {snippet}")

    def test_ack_with_conditional_interview_mention(self):
        self.check("Thank you for applying to Acme", "We received your application. If you are selected for an interview we will be in touch.", "ack")

    def test_ack_that_cannot_reply_to_all(self):
        self.check("Application received", "Unfortunately we cannot respond to every applicant individually.", "ack")

    def test_rejection(self):
        self.check("Your application to Acme", "We have decided to move forward with other candidates.", "rejection")
        self.check("Update", "Unfortunately, we will not be moving forward with your application.", "rejection")

    def test_interview(self):
        self.check("Interview invitation", "Hi, we'd like to meet you.", "interview_invite")
        self.check("Next steps", "Please share your availability this week for a call.", "interview_invite")
        self.check("Hello", "Book a time here: calendly.com/recruiter/30min", "interview_invite")

    def test_offer(self):
        self.check("Offer of employment", "We are pleased to offer you the role.", "offer")

    def test_plain_reply(self):
        self.check("Question about your CV", "Which frameworks have you used?", "reply")


class MatchApplicationTests(unittest.TestCase):
    APPS = [
        {"id": 1, "company": "Fynbos Tech (Pty) Ltd", "title": "Graduate Developer", "thread_id": ""},
        {"id": 2, "company": "Orbit Payments Inc.", "title": "Backend Developer", "thread_id": "T99"},
    ]

    def msg(self, sender, subject, snippet="", thread=""):
        return {"from": sender, "subject": subject, "snippet": snippet, "thread_id": thread}

    def test_by_sender_display_name(self):
        app = tracker.match_application(self.msg("Thandi at Fynbos Tech <talent@mail.example.com>", "Hello"), self.APPS)
        self.assertEqual(app["id"], 1)

    def test_by_domain(self):
        app = tracker.match_application(self.msg("recruiter <jobs@orbit-payments.com>", "Hello"), self.APPS)
        self.assertEqual(app["id"], 2)

    def test_by_thread(self):
        app = tracker.match_application(self.msg("x <x@x.com>", "Re: hi", thread="T99"), self.APPS)
        self.assertEqual(app["id"], 2)

    def test_ats_sender_matched_via_subject(self):
        app = tracker.match_application(self.msg("Greenhouse <no-reply@greenhouse-mail.io>", "Thanks for applying to Fynbos Tech"), self.APPS)
        self.assertEqual(app["id"], 1)

    def test_unrelated_mail_ignored(self):
        self.assertIsNone(tracker.match_application(self.msg("Newsletter <hi@news.example.com>", "Weekly digest"), self.APPS))

    def test_company_only_in_snippet_needs_title(self):
        self.assertIsNone(tracker.match_application(self.msg("a <a@b.com>", "Digest", "Fynbos Tech raised funding"), self.APPS))


class DatabaseFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()

    def new_app(self, company, title="Dev", status="applied", applied_days=3):
        jid = db.execute(
            "INSERT INTO jobs (source, external_id, title, company, found_at, status) VALUES ('t', ?, ?, ?, ?, 'matched')",
            (f"{company}{title}{status}{applied_days}", title, company, db.now()))
        applied = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=applied_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return db.execute(
            "INSERT INTO applications (job_id, status, created_at, applied_at) VALUES (?,?,?,?)", (jid, status, db.now(), applied))

    def status(self, app_id):
        return db.one("SELECT status FROM applications WHERE id=?", (app_id,))["status"]

    def test_transitions_only_move_forward(self):
        a = self.new_app("TransCo")
        at = db.now()
        self.assertEqual(tracker.apply_transition(a, "applied", "ack", at), "acknowledged")
        self.assertEqual(tracker.apply_transition(a, "acknowledged", "interview_invite", at), "interviewing")
        # a later auto-reply must not move an interviewing application backwards
        self.assertEqual(tracker.apply_transition(a, "interviewing", "ack", at), "interviewing")
        self.assertEqual(self.status(a), "interviewing")
        self.assertIsNotNone(db.one("SELECT first_response_at FROM applications WHERE id=?", (a,))["first_response_at"])

    def test_ack_is_not_a_response(self):
        a = self.new_app("AckCo")
        tracker.apply_transition(a, "applied", "ack", db.now())
        self.assertIsNone(db.one("SELECT first_response_at FROM applications WHERE id=?", (a,))["first_response_at"])

    def test_rejection_sticks_and_offer_survives(self):
        a = self.new_app("RejCo")
        tracker.apply_transition(a, "applied", "rejection", db.now())
        self.assertEqual(self.status(a), "rejected")
        tracker.apply_transition(a, "rejected", "ack", db.now())
        self.assertEqual(self.status(a), "rejected")
        b = self.new_app("OfferCo")
        tracker.apply_transition(b, "applied", "offer", db.now())
        tracker.apply_transition(b, "offer", "rejection", db.now())
        self.assertEqual(self.status(b), "offer")

    def test_ghosting(self):
        old = self.new_app("OldCo", applied_days=20)
        fresh = self.new_app("FreshCo", applied_days=2)
        replied = self.new_app("RepliedCo", applied_days=20)
        db.execute("UPDATE applications SET first_response_at=? WHERE id=?", (db.now(), replied))
        tracker.mark_ghosted()
        self.assertEqual(self.status(old), "ghosted")
        self.assertEqual(self.status(fresh), "applied")
        self.assertEqual(self.status(replied), "applied")
        # a reply later revives a ghosted application
        tracker.apply_transition(old, "ghosted", "reply", db.now())
        self.assertEqual(self.status(old), "replied")

    def test_add_event_dedupes_gmail_ids(self):
        a = self.new_app("DupCo")
        self.assertIsNotNone(db.add_event(a, "reply", "x", gmail_id="msg-1"))
        self.assertIsNone(db.add_event(a, "reply", "x", gmail_id="msg-1"))


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()

    def cfg(self, **apply):
        cfg = config.load_config()
        cfg["apply"].update(apply)
        return cfg

    def test_ingest_scores_queues_and_dedupes(self):
        cfg = self.cfg(mode="auto_strong")
        recent = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        jobs = [
            Job("t", "1", "Junior Python Developer", "PipeCo A", "Cape Town", False, "u1", "u1",
                description="Python, SQL, Azure, git", posted_at=recent),
            Job("t", "2", "Software Engineer", "PipeCo B", "Berlin", False, "u2", "u2", description="python sql", posted_at=recent),
            Job("t", "3", "Senior Python Engineer", "PipeCo C", "Cape Town", False, "u3", "u3", description="python", posted_at=recent),
            Job("t", "4", "Junior Python Developer", "PipeCo A", "Cape Town", False, "u4", "u4", description="python", posted_at=recent),
            Job("t", "5", "Python Developer", "PipeCo D", "Cape Town", False, "u5", "u5", description="python sql",
                posted_at="2020-01-01T00:00:00Z"),
        ]
        out = pipeline.ingest(jobs, cfg)
        self.assertEqual(out["new"], 4)  # the 2020 posting is ignored as too old
        a = db.one("SELECT a.status, j.score FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.external_id='1'")
        self.assertEqual(a["status"], "queued")  # strong match -> auto queue
        self.assertGreaterEqual(a["score"], 75)
        self.assertEqual(db.one("SELECT status FROM jobs WHERE external_id='3'")["status"], "skipped")
        self.assertEqual(db.one("SELECT status FROM jobs WHERE external_id='4'")["status"], "duplicate")
        self.assertIsNone(db.one("SELECT 1 FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.external_id='4'"))
        # running the same batch again adds nothing
        self.assertEqual(pipeline.ingest(jobs, cfg)["new"], 0)

    def test_approve_all_mode_never_auto_queues(self):
        cfg = self.cfg(mode="approve_all")
        recent = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        pipeline.ingest([Job("t", "10", "Junior Python Developer", "ApproveCo", "Cape Town", False, "u", "u",
                             description="Python, SQL, Azure, git", posted_at=recent)], cfg)
        row = db.one("SELECT a.status FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.external_id='10'")
        self.assertEqual(row["status"], "review")

    def test_company_limit(self):
        cfg = self.cfg(mode="approve_all", max_per_company=1)
        recent = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        mk = lambda i, t: Job("t", i, t, "LimitCo", "Cape Town", False, "u", "u", description="python sql git", posted_at=recent)
        pipeline.ingest([mk("20", "Junior Python Developer"), mk("21", "Graduate Software Engineer")], cfg)
        n = db.one("SELECT COUNT(*) n FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.company='LimitCo'")["n"]
        self.assertEqual(n, 1)


class ApplierTests(unittest.TestCase):
    def test_field_classification(self):
        cases = {
            "First Name *": "first_name", "Last name": "last_name", "Full name": "full_name", "Name": "full_name",
            "Email address": "email", "Phone number": "phone", "Mobile": "phone",
            "LinkedIn profile": "linkedin", "GitHub URL": "github", "Portfolio / website": "website",
            "Cover letter": "cover_letter", "Current company": "current_company", "Current location": "location",
            "Expected salary": None, "Are you authorised to work in South Africa?": None,
        }
        for label, expected in cases.items():
            self.assertEqual(applier.classify_field(label), expected, label)

    def test_target_kind(self):
        self.assertEqual(applier.target_kind({"apply_url": "https://boards.greenhouse.io/x/jobs/1", "url": "", "apply_email": ""}), "greenhouse")
        self.assertEqual(applier.target_kind({"apply_url": "https://jobs.lever.co/x/1", "url": "", "apply_email": ""}), "lever")
        self.assertEqual(applier.target_kind({"apply_url": "https://x.com/1", "url": "", "apply_email": "jobs@x.com"}), "email")
        self.assertEqual(applier.target_kind({"apply_url": "https://www.linkedin.com/jobs/1", "url": "", "apply_email": ""}), "manual")

    def test_cover_letter(self):
        cfg = config.load_config()
        cfg["profile"].update(first_name="Ada", last_name="Lovelace", email="ada@example.com")
        text = applier.build_cover_letter(cfg, {"title": "Python Developer", "company": "Acme", "description": "python and sql"})
        self.assertIn("Acme", text)
        self.assertIn("Python Developer", text)
        self.assertIn("python and sql", text)
        self.assertIn("Ada Lovelace", text)
        self.assertNotIn("{", text)

    def test_apply_refuses_without_profile(self):
        cfg = config.load_config()
        cfg["profile"].update(first_name="", last_name="", email="")
        result = applier.apply({"apply_url": "https://boards.greenhouse.io/a/jobs/1", "url": "", "apply_email": "", "title": "t", "company": "c", "description": ""},
                               {"id": 1, "cover_letter": ""}, cfg, submit=True)
        self.assertEqual(result["status"], "manual")

    def test_apply_email_extraction(self):
        self.assertEqual(find_apply_email("Send your CV to careers@acme.co.za today"), "careers@acme.co.za")
        self.assertEqual(find_apply_email("Contact john.smith@acme.com"), "")


class ConfigAndCryptoTests(unittest.TestCase):
    def test_validation(self):
        self.assertIsNone(config.validate_config_text("apply:\n  mode: off\n")[0])
        self.assertIn("mode", config.validate_config_text("apply:\n  mode: sometimes\n")[0])
        self.assertIn("list", config.validate_config_text("search:\n  titles: just text\n")[0])
        self.assertIn("YAML", config.validate_config_text("a: [unclosed")[0])
        self.assertIn("number", config.validate_config_text("apply:\n  daily_limit: lots\n")[0])

    def test_encryption_roundtrip(self):
        token = crypto.encrypt("refresh-token-123")
        self.assertNotIn("refresh-token", token)
        self.assertEqual(crypto.decrypt(token), "refresh-token-123")
        self.assertIsNone(crypto.decrypt("garbage"))

    def test_secret_files_are_private(self):
        from jobagent import security
        security.access_token()
        mode = (config.DATA_DIR / ".access_token").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
