"""Sample data so you can look around the dashboard before connecting anything."""
from __future__ import annotations

import datetime as dt

from . import db, pipeline


def _ago(days: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _later(days: float, hour: int = 10) -> str:
    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
    return when.replace(hour=hour, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _job(title: str, company: str, location: str, score: int, reasons: str, url: str, status="matched") -> int:
    return db.execute(
        "INSERT INTO jobs (source, external_id, title, company, location, remote, url, apply_url, description, found_at,"
        " score, reasons, dedupe_key, status) VALUES ('demo', ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"{company}-{title}", title, company, location, url, url,
         f"{company} is hiring a {title}. You will work with Python, SQL and Azure in a small team.",
         _ago(1), score, reasons, pipeline.dedupe_key(company, title), status))


def _app(job_id: int, status: str, applied_days: float | None, first_response_days: float | None = None, **kw) -> int:
    return db.execute(
        "INSERT INTO applications (job_id, status, method, created_at, applied_at, last_event_at, first_response_at, notes)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (job_id, status, kw.get("method", "greenhouse"), _ago((applied_days or 1) + 0.1),
         _ago(applied_days) if applied_days is not None else None, _ago(kw.get("last", applied_days or 1)),
         _ago(first_response_days) if first_response_days is not None else None, kw.get("notes", "")))


def seed() -> None:
    if db.one("SELECT 1 FROM jobs"):
        return
    # Waiting for approval
    for title, company, loc, score in (
        ("Junior Python Developer", "Lumen Labs", "Cape Town, South Africa", 82),
        ("Graduate Software Engineer", "Harbour Systems", "Remote", 71),
        ("Software Developer Intern", "Kelp Analytics", "Stellenbosch, South Africa", 58),
    ):
        job = _job(title, company, loc, score, "title matches 'software'; skills: python, sql; location ok",
                   "https://example.com/jobs/" + company.split()[0].lower(), status="matched")
        db.execute("INSERT INTO applications (job_id, status, created_at, cover_letter) VALUES (?, 'review', ?, ?)",
                   (job, _ago(0.2), "Dear hiring team, ..."))

    # Applied / progressing
    rows = [
        ("Software Engineer", "Northwind Digital", 78, "applied", 3, None),
        ("Junior .NET Developer", "Bluepeak Software", 88, "acknowledged", 6, None),
        ("Backend Developer", "Orbit Payments", 81, "replied", 9, 7),
        ("Graduate Developer", "Fynbos Tech", 90, "interviewing", 12, 9),
        ("Python Developer", "Tidewater Data", 76, "rejected", 15, 11),
        ("Software Developer", "Cobalt Retail", 69, "ghosted", 24, None),
        ("Full-stack Developer", "Karoo Cloud", 84, "ghosted", 30, None),
        ("Software Engineer (Junior)", "Signal Hill Games", 73, "offer", 20, 14),
    ]
    app_ids = {}
    for title, company, score, status, applied, resp in rows:
        job = _job(title, company, "Cape Town, South Africa", score, "title matches; skills: python, c#", f"https://example.com/{company}")
        app_ids[company] = _app(job, status, applied, resp)
        db.add_event(app_ids[company], "applied", "Applied via greenhouse", at=_ago(applied))
        if status != "applied":
            db.add_event(app_ids[company], "ack", "Application receipt / auto-reply",
                         "From: no-reply@greenhouse-mail.io\nSubject: Thank you for applying\n\nWe received your application.",
                         at=_ago(applied - 0.01))
    db.add_event(app_ids["Orbit Payments"], "reply", "Reply from the company",
                 "From: Priya at Orbit Payments\nSubject: Quick question about your CV\n\nCould you tell us which Python frameworks you have used?", at=_ago(7))
    db.add_event(app_ids["Fynbos Tech"], "interview_invite", "Looks like an interview invitation",
                 "From: talent@fynbos.example\nSubject: Interview with Fynbos Tech\n\nWe would like to schedule an interview.", at=_ago(9))
    db.add_event(app_ids["Tidewater Data"], "rejection", "Looks like a rejection",
                 "From: Tidewater Data\nSubject: Your application\n\nWe have decided to move forward with other candidates.", at=_ago(11))
    db.add_event(app_ids["Signal Hill Games"], "offer", "Looks like a job offer",
                 "From: People Team\nSubject: Offer of employment\n\nWe are pleased to offer you the position.", at=_ago(14))
    db.add_event(app_ids["Cobalt Retail"], "ghosted", "No reply after 14 days", at=_ago(10))
    db.add_event(app_ids["Karoo Cloud"], "ghosted", "No reply after 14 days", at=_ago(16))

    db.execute("INSERT INTO interviews (application_id, scheduled_at, kind, link, interviewer, notes) VALUES (?,?,?,?,?,?)",
               (app_ids["Fynbos Tech"], _later(2, 8), "Phone screen", "https://meet.example.com/fynbos", "Thandi (recruiter)",
                "Be ready to talk about the group project."))
    db.execute("INSERT INTO interviews (application_id, scheduled_at, kind, link, interviewer, notes) VALUES (?,?,?,?,?,?)",
               (app_ids["Fynbos Tech"], _later(6, 13), "Technical interview", "", "Dev team", ""))
    db.execute("INSERT INTO interviews (application_id, scheduled_at, kind, outcome) VALUES (?,?,?,?)",
               (app_ids["Signal Hill Games"], _ago(16), "Technical interview", "passed"))

    # A manual-apply case and a below-threshold job
    manual = _job("Developer", "Granite Group", "Johannesburg", 66, "skills: sql, python", "https://example.com/granite")
    app_manual = _app(manual, "manual", None)
    db.execute("UPDATE applications SET error=? WHERE id=?", ("This posting is on a site that can't be automated safely. Open it and apply there.", app_manual))
    _job("Senior Staff Engineer", "BigCorp", "London", 0, "excluded: title contains 'senior'", "https://example.com/bigcorp", status="skipped")
    db.log("info", "demo data loaded")
    db.kv_set("last_search", _ago(0.1))
