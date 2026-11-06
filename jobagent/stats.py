"""Numbers for the dashboard."""
from __future__ import annotations

import datetime as dt

from . import db


def overview() -> dict:
    by_status = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM applications GROUP BY status")}
    sent = db.one("SELECT COUNT(*) n FROM applications WHERE applied_at IS NOT NULL")["n"]
    responded = db.one("SELECT COUNT(*) n FROM applications WHERE first_response_at IS NOT NULL")["n"]
    interviewed = db.one(
        "SELECT COUNT(DISTINCT application_id) n FROM ("
        " SELECT application_id FROM events WHERE kind='interview_invite'"
        " UNION SELECT application_id FROM interviews"
        " UNION SELECT id FROM applications WHERE status='interviewing')")["n"]
    avg = db.one(
        "SELECT AVG(julianday(first_response_at) - julianday(applied_at)) d FROM applications "
        "WHERE first_response_at IS NOT NULL AND applied_at IS NOT NULL")["d"]
    silent = by_status.get("ghosted", 0)
    return {
        "by_status": by_status,
        "sent": sent,
        "responded": responded,
        "response_rate": round(100 * responded / sent) if sent else None,
        "interviewed": interviewed,
        "offers": by_status.get("offer", 0),
        "rejected": by_status.get("rejected", 0),
        "silent": silent,
        "avg_days_to_reply": round(avg, 1) if avg is not None else None,
        "review": by_status.get("review", 0),
    }


def weekly_applied(weeks: int = 8) -> list[dict]:
    today = dt.datetime.now(dt.timezone.utc).date()
    monday = today - dt.timedelta(days=today.weekday())
    out = []
    for i in range(weeks - 1, -1, -1):
        start = monday - dt.timedelta(weeks=i)
        end = start + dt.timedelta(days=7)
        n = db.one("SELECT COUNT(*) n FROM applications WHERE applied_at >= ? AND applied_at < ?",
                   (start.isoformat() + "T00:00:00Z", end.isoformat() + "T00:00:00Z"))["n"]
        out.append({"label": start.strftime("%d %b"), "n": n})
    peak = max((w["n"] for w in out), default=0) or 1
    for w in out:
        w["pct"] = round(100 * w["n"] / peak)
    return out


def attention() -> list[dict]:
    """Things that need the user."""
    items = []
    n = db.one("SELECT COUNT(*) n FROM applications WHERE status='review'")["n"]
    if n:
        items.append({"text": f"{n} job{'s' if n != 1 else ''} to review", "href": "/review", "tone": "go"})
    n = db.one("SELECT COUNT(*) n FROM applications WHERE status='ready'")["n"]
    if n:
        items.append({"text": f"{n} application{'s' if n != 1 else ''} prepared and ready to submit",
                      "href": "/applications?status=ready", "tone": "go"})
    for row in db.query(
            "SELECT a.id, j.company, j.title FROM applications a JOIN jobs j ON j.id=a.job_id "
            "WHERE a.status='interviewing' AND NOT EXISTS (SELECT 1 FROM interviews i WHERE i.application_id=a.id)"):
        items.append({"text": f"{row['company']} replied about an interview: add the details",
                      "href": f"/applications/{row['id']}", "tone": "warn"})
    n = db.one("SELECT COUNT(*) n FROM applications WHERE status='manual'")["n"]
    if n:
        items.append({"text": f"{n} application{'s' if n != 1 else ''} need a manual step",
                      "href": "/applications?status=manual", "tone": "warn"})
    n = db.one("SELECT COUNT(*) n FROM applications WHERE status='failed'")["n"]
    if n:
        items.append({"text": f"{n} application{'s' if n != 1 else ''} failed to send",
                      "href": "/applications?status=failed", "tone": "bad"})
    return items
