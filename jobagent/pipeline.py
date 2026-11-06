"""The search -> score -> queue -> apply pipeline."""
from __future__ import annotations

import datetime as dt
import re
import threading
import time

from . import applier, db, sources
from .config import load_config
from .matcher import score_job

_locks = {"search": threading.Lock(), "apply": threading.Lock()}


def dedupe_key(company: str, title: str) -> str:
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return f"{norm(company)}|{norm(title)}"


def _too_old(posted_at: str, max_days: int) -> bool:
    if not posted_at:
        return False
    try:
        posted = dt.datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (dt.datetime.now(dt.timezone.utc) - posted).days > max_days


def _company_count(company: str) -> int:
    row = db.one(
        "SELECT COUNT(*) n FROM applications a JOIN jobs j ON j.id=a.job_id "
        "WHERE lower(j.company)=lower(?) AND a.status NOT IN ('dismissed','withdrawn','failed')", (company,))
    return row["n"]


def queue_job(job_id: int, cfg: dict, status: str | None = None) -> int | None:
    """Create the application row for a job. status: 'queued' or 'review' (default chosen from score)."""
    job = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job or db.one("SELECT 1 FROM applications WHERE job_id=?", (job_id,)):
        return None
    if status is None:
        auto = (cfg["apply"]["mode"] == "auto_strong" and job["score"] >= cfg["search"]["auto_apply_score"])
        status = "queued" if auto else "review"
    app_id = db.execute(
        "INSERT INTO applications (job_id, status, created_at, cover_letter) VALUES (?,?,?,?)",
        (job_id, status, db.now(), applier.build_cover_letter(cfg, job)))
    db.execute("UPDATE jobs SET status='matched' WHERE id=?", (job_id,))
    db.add_event(app_id, "found", f"Matched with score {job['score']}", job["reasons"] or "")
    return app_id


def run_search() -> dict:
    if not _locks["search"].acquire(blocking=False):
        return {"skipped": "a search is already running"}
    try:
        cfg = load_config()
        fetched = sources.fetch_all(cfg)
        return ingest(fetched, cfg)
    finally:
        _locks["search"].release()


def ingest(fetched: list, cfg: dict) -> dict:
    """Score and store freshly fetched jobs, queueing the good ones."""
    new = queued = skipped = 0
    search = cfg["search"]
    for job in fetched:
        if not job.title or not job.company or _too_old(job.posted_at, int(search["max_age_days"])):
            continue
        if db.one("SELECT 1 FROM jobs WHERE source=? AND external_id=?", (job.source, job.external_id)):
            continue
        key = dedupe_key(job.company, job.title)
        score, reasons = score_job(
            {"title": job.title, "description": job.description, "location": job.location, "remote": job.remote}, search)
        duplicate = db.one("SELECT 1 FROM jobs WHERE dedupe_key=?", (key,)) is not None
        status = "duplicate" if duplicate else ("new" if score >= int(search["min_score"]) else "skipped")
        job_id = db.execute(
            "INSERT INTO jobs (source, external_id, title, company, location, remote, url, apply_url, apply_email,"
            " description, posted_at, found_at, score, reasons, dedupe_key, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job.source, job.external_id, job.title, job.company, job.location, int(job.remote), job.url,
             job.apply_url, job.apply_email, job.description[:20000], job.posted_at, db.now(), score,
             "; ".join(reasons), key, status))
        new += 1
        if status == "skipped" or status == "duplicate":
            skipped += 1
            continue
        if _company_count(job.company) >= int(cfg["apply"]["max_per_company"]):
            db.execute("UPDATE jobs SET status='skipped', reasons=reasons || '; company limit reached' WHERE id=?", (job_id,))
            skipped += 1
            continue
        if queue_job(job_id, cfg):
            queued += 1
    db.log("info", f"search: {new} new postings, {queued} queued, {skipped} below threshold")
    db.kv_set("last_search", db.now())
    return {"new": new, "queued": queued, "skipped": skipped}


def rescore_all() -> int:
    """Re-score stored jobs after the search description changed."""
    cfg = load_config()
    changed = 0
    for job in db.query("SELECT * FROM jobs WHERE status IN ('skipped','new','matched')"):
        score, reasons = score_job(job, cfg["search"])
        db.execute("UPDATE jobs SET score=?, reasons=? WHERE id=?", (score, "; ".join(reasons), job["id"]))
        if (job["status"] == "skipped" and score >= int(cfg["search"]["min_score"])
                and _company_count(job["company"]) < int(cfg["apply"]["max_per_company"])):
            queue_job(job["id"], cfg)
            changed += 1
    return changed


# ------------------------------------------------------------------------ apply

def record_result(app_id: int, result: dict) -> None:
    status = result["status"]
    fields = {"method": result.get("method", ""), "error": result.get("error", ""),
              "screenshot": result.get("screenshot", "")}
    if result.get("thread_id"):
        fields["thread_id"] = result["thread_id"]
    if status == "applied":
        fields["applied_at"] = db.now()
        db.set_status(app_id, "applied", **fields)
        db.add_event(app_id, "applied", f"Applied via {result.get('method') or 'link'}")
        from . import tracker
        tracker.reset_seen()
    elif status == "ready":
        db.set_status(app_id, "ready", **fields)
        db.add_event(app_id, "prepared", "Form filled in, waiting for your OK to submit")
    elif status == "manual":
        db.set_status(app_id, "manual", **fields)
        db.add_event(app_id, "manual", result.get("error", "Needs a manual step"))
    else:
        db.set_status(app_id, "failed", **fields)
        db.add_event(app_id, "failed", result.get("error", "Failed"))


def apply_one(app_id: int, force_submit: bool = False) -> dict:
    cfg = load_config()
    row = db.one("SELECT * FROM applications WHERE id=?", (app_id,))
    job = db.one("SELECT j.* FROM jobs j JOIN applications a ON a.job_id=j.id WHERE a.id=?", (app_id,))
    if not row or not job:
        return {"error": "not found"}
    submit = force_submit or not cfg["apply"]["dry_run"]
    db.log("info", f"apply: {job['company']} - {job['title']} ({'submit' if submit else 'prepare'})")
    result = applier.apply(job, row, cfg, submit)
    record_result(app_id, result)
    return result


def _sent_today() -> int:
    cfg = load_config()
    start = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    row = db.one("SELECT COUNT(*) n FROM applications WHERE applied_at >= ?", (start.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    return row["n"]


def run_apply_queue() -> dict:
    if not _locks["apply"].acquire(blocking=False):
        return {"skipped": "already applying"}
    try:
        cfg = load_config()
        if cfg["apply"]["mode"] == "off":
            return {"skipped": "apply mode is off"}
        remaining = int(cfg["apply"]["daily_limit"]) - _sent_today()
        dry = bool(cfg["apply"]["dry_run"])
        done = 0
        for row in db.query(
                "SELECT a.id FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.status='queued' ORDER BY j.score DESC"):
            if not dry and remaining <= 0:
                db.log("info", "apply: daily limit reached")
                break
            result = apply_one(row["id"])
            done += 1
            if result.get("status") == "applied":
                remaining -= 1
            time.sleep(int(cfg["apply"]["delay_seconds"]) if result.get("status") in ("applied", "ready") else 1)
        return {"processed": done}
    finally:
        _locks["apply"].release()
