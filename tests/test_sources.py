"""Connector tests using fake API responses (shapes follow each service's public docs)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("JOBAGENT_DATA", tempfile.mkdtemp(prefix="jobagent-src-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from jobagent import config, db, sources  # noqa: E402

db.init()
CFG = config.load_config()


class FakeResponse:
    def __init__(self, payload=None, content=b"", status=200):
        self._payload, self.content, self.status_code = payload, content, status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


def fake_get(routes):
    """routes: {url substring: FakeResponse}; first matching substring wins."""
    calls = []

    def _get(url, params=None):
        calls.append((url, params))
        for key, response in routes.items():
            if key in url:
                response.raise_for_status()  # like the real _get
                return response
        raise AssertionError(f"unexpected request: {url}")
    _get.calls = calls
    return _get


class SourceTests(unittest.TestCase):
    def setUp(self):
        self._orig = sources._get

    def tearDown(self):
        sources._get = self._orig

    def use(self, routes):
        sources._get = fake_get(routes)
        return sources._get

    def test_ashby(self):
        self.use({"posting-api/job-board/linear": FakeResponse({"jobs": [
            {"id": "a1", "title": "Software Engineer", "location": "Remote - EMEA", "isRemote": True, "isListed": True,
             "jobUrl": "https://jobs.ashbyhq.com/linear/a1", "applyUrl": "https://jobs.ashbyhq.com/linear/a1/application",
             "descriptionPlain": "We use Python and SQL.", "publishedAt": "2026-10-30T09:00:00.000+00:00"},
            {"id": "a2", "title": "Hidden", "isListed": False},
        ]})})
        jobs = sources.fetch_ashby({**CFG, "sources": {**CFG["sources"], "ashby": ["linear"]}})
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual((j.source, j.company, j.title, j.remote), ("ashby", "Linear", "Software Engineer", True))
        self.assertEqual(j.apply_url, "https://jobs.ashbyhq.com/linear/a1/application")
        self.assertTrue(j.posted_at.startswith("2026-10-30"))

    def test_workable_dedupes_locations(self):
        self.use({"accounts/canonical": FakeResponse({"name": "Canonical", "jobs": [
            {"shortcode": "ABC", "title": "Python Developer", "city": "Cape Town", "country": "South Africa", "telecommuting": False,
             "url": "https://apply.workable.com/j/ABC", "application_url": "https://apply.workable.com/j/ABC/apply",
             "description": "<p>Python</p>", "published_on": "2026-10-28"},
            {"shortcode": "ABC", "title": "Python Developer", "city": "Lagos", "country": "Nigeria"},
        ]})})
        jobs = sources.fetch_workable({**CFG, "sources": {**CFG["sources"], "workable": ["canonical"]}})
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0].company, jobs[0].location), ("Canonical", "Cape Town, South Africa"))
        self.assertEqual(jobs[0].description, "Python")
        self.assertTrue(jobs[0].posted_at.startswith("2026-10-28"))

    def test_smartrecruiters_skips_implausible_titles_and_fetches_details(self):
        listing = {"totalFound": 2, "content": [
            {"id": "1", "name": "Junior Software Developer", "company": {"name": "Visa"}, "releasedDate": "2026-10-20T08:00:00.000Z",
             "location": {"city": "Cape Town", "country": "za", "remote": False, "fullLocation": "Cape Town, South Africa"}},
            {"id": "2", "name": "Senior Marketing Manager", "company": {"name": "Visa"}, "location": {}},
        ]}
        detail = {"postingUrl": "https://jobs.smartrecruiters.com/Visa/1-junior", "jobAd": {"sections": {
            "jobDescription": {"text": "<p>Build things in Python.</p>"}, "qualifications": {"text": "SQL"}}}}
        get = self.use({"postings/1": FakeResponse(detail), "/postings": FakeResponse(listing)})
        jobs = sources.fetch_smartrecruiters({**CFG, "sources": {**CFG["sources"], "smartrecruiters": ["Visa"]}})
        self.assertEqual([j.title for j in jobs], ["Junior Software Developer"])
        self.assertIn("Python", jobs[0].description)
        self.assertEqual(jobs[0].url, "https://jobs.smartrecruiters.com/Visa/1-junior")
        self.assertEqual(sum("postings/2" in c[0] for c in get.calls), 0)

    def test_smartrecruiters_empty_company_is_logged_not_fatal(self):
        self.use({"/postings": FakeResponse({"totalFound": 0, "content": []})})
        self.assertEqual(sources.fetch_smartrecruiters({**CFG, "sources": {**CFG["sources"], "smartrecruiters": ["Nope"]}}), [])
        self.assertTrue(any("no postings" in r["message"] for r in db.query("SELECT message FROM logs")))

    def test_bad_company_does_not_stop_the_others(self):
        self.use({"board/ghost": FakeResponse(status=404),
                  "board/linear": FakeResponse({"jobs": [{"id": "z", "title": "Dev", "jobUrl": "u"}]})})
        jobs = sources.fetch_ashby({**CFG, "sources": {**CFG["sources"], "ashby": ["ghost", "linear"]}})
        self.assertEqual(len(jobs), 1)
        self.assertTrue(any("ghost" in r["message"] and "404" in r["message"] for r in db.query("SELECT message FROM logs")))

    def test_himalayas_pages_until_empty(self):
        page = {"jobs": [{"title": "Backend Developer", "companyName": "Acme", "guid": "https://himalayas.app/jobs/1",
                          "applicationLink": "https://himalayas.app/apply/1", "locationRestrictions": ["United States"],
                          "description": "<b>Python</b>", "pubDate": 1793000000}]}
        responses = iter([FakeResponse(page), FakeResponse({"jobs": []})])
        sources._get = lambda url, params=None: next(responses)
        jobs = sources.fetch_himalayas(CFG)
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0].location, jobs[0].remote, jobs[0].description), ("United States", True, "Python"))
        self.assertTrue(jobs[0].posted_at.startswith("2026"))
        # no restrictions -> worldwide
        sources._get = lambda url, params=None: FakeResponse({"jobs": []} if params["offset"] else {"jobs": [{"title": "T", "companyName": "C"}]})
        self.assertEqual(sources.fetch_himalayas(CFG)[0].location, "Worldwide")

    def test_remoteok_skips_legal_notice_and_uses_tags(self):
        self.use({"remoteok.com/api": FakeResponse([
            {"legal": "Please link back to Remote OK"},
            {"id": "99", "position": "Python Engineer", "company": "Foo", "location": "Worldwide", "tags": ["python", "django"],
             "description": "<p>Great job</p>", "url": "https://remoteok.com/remote-jobs/99", "epoch": 1793000000}])})
        jobs = sources.fetch_remoteok(CFG)
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0].title, jobs[0].company), ("Python Engineer", "Foo"))
        self.assertIn("django", jobs[0].description)

    def test_weworkremotely_rss(self):
        rss = b"""<?xml version="1.0"?><rss><channel>
          <item><title>Acme Inc: Junior Python Developer</title><region>Anywhere in the World</region>
            <description>&lt;p&gt;Python and SQL&lt;/p&gt;</description><pubDate>Thu, 29 Oct 2026 10:00:00 +0000</pubDate>
            <link>https://weworkremotely.com/remote-jobs/acme-junior</link><guid>https://weworkremotely.com/remote-jobs/acme-junior</guid></item>
          </channel></rss>"""
        self.use({"remote-jobs.rss": FakeResponse(content=rss)})
        jobs = sources.fetch_weworkremotely(CFG)
        self.assertEqual((jobs[0].company, jobs[0].title, jobs[0].location), ("Acme Inc", "Junior Python Developer", "Anywhere in the World"))
        self.assertEqual(jobs[0].posted_at, "2026-10-29T10:00:00Z")
        self.assertEqual(jobs[0].description, "Python and SQL")

    def test_weworkremotely_refuses_entity_definitions(self):
        evil = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "x">]><rss><channel><item><title>&a;</title></item></channel></rss>'
        self.use({"remote-jobs.rss": FakeResponse(content=evil)})
        with self.assertRaises(ValueError):
            sources.fetch_weworkremotely(CFG)

    def test_fetch_all_isolates_failures_and_honours_toggles(self):
        cfg = {**CFG, "sources": {**CFG["sources"], "remotive": False, "arbeitnow": False, "himalayas": False,
                                  "remoteok": True, "weworkremotely": True}}

        def _get(url, params=None):
            if "remoteok" in url:
                raise requests.ConnectionError("boom")
            return FakeResponse(content=b"<rss><channel/></rss>")
        sources._get = _get
        self.assertEqual(sources.fetch_all(cfg), [])
        self.assertTrue(any("remoteok" in r["message"] and "boom" in r["message"] for r in db.query("SELECT message FROM logs")))

    def test_to_iso_formats(self):
        self.assertEqual(sources.to_iso("2026-10-30"), "2026-10-30T00:00:00Z")
        self.assertEqual(sources.to_iso("Thu, 29 Oct 2026 10:00:00 +0200"), "2026-10-29T08:00:00Z")
        self.assertEqual(sources.to_iso(1793000000), sources.to_iso("1793000000"))
        self.assertEqual(sources.to_iso("garbage"), "")
        self.assertEqual(sources.to_iso(None), "")


if __name__ == "__main__":
    unittest.main()
