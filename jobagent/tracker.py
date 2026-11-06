"""Turn inbox messages into application progress: replies, interviews, rejections, ghosting."""
from __future__ import annotations

import datetime as dt
import re
import threading
from email.utils import parseaddr

from . import db, gmail
from .config import load_config
from .constants import EVENT_TO_STATUS, SENT_STATUSES, STATUS_RANK

_lock = threading.Lock()

_OFFER = re.compile(
    r"(pleased to offer|happy to offer|excited to offer|delighted to offer|job offer|offer of employment|"
    r"offer letter|extend (you )?an offer|formal offer)", re.I)
_REJECT = re.compile(
    r"(regret to inform|(not|n't) (be )?(moving|proceeding|going|continuing) forward|"
    r"(move|moving|proceed|proceeding|go|going|pursue|pursuing) forward with (other|another)|"
    r"(chosen|selected|decided on|opted for) (to move forward with )?(another|other)|"
    r"position (has been|was) filled|not (been )?selected|unsuccessful|"
    r"will not be (progressing|proceeding|taking)|unable to offer you|no longer (being )?considered|"
    r"decided not to (proceed|continue|move))", re.I)
_STRONG_INTERVIEW = re.compile(
    r"(invite you|would like to (schedule|arrange|set up|speak|chat|meet|interview)|"
    r"schedule (an? )?(interview|call|chat|time)|book (a )?(time|slot|call|interview)|calendly\.com|"
    r"phone screen|screening call|(technical|coding) (assessment|test|challenge)|take-?home|"
    r"(share|confirm|send|provide|let us know) your availability|your availability)", re.I)
_ACK = re.compile(
    r"(thank(s| you) for (applying|your application|your interest|submitting)|"
    r"we('ve| have) received your application|application (has been )?(received|submitted)|"
    r"successfully (applied|submitted)|your application (to|for|was))", re.I)

_SUFFIX = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|pty|plc|gmbh|corp|corporation|co|company|group|holdings|the|sa|ag|bv)\b", re.I)


def classify(subject: str, snippet: str) -> tuple[str, str]:
    """Return (event kind, short summary)."""
    text = f"{subject} {snippet}"
    if _OFFER.search(text):
        return "offer", "Looks like a job offer"
    if _REJECT.search(text):
        return "rejection", "Looks like a rejection"
    if _STRONG_INTERVIEW.search(text) or re.search(r"interview", subject, re.I):
        return "interview_invite", "Looks like an interview invitation"
    if _ACK.search(text):
        return "ack", "Application receipt / auto-reply"
    return "reply", "Reply from the company"


def norm_company(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", _SUFFIX.sub(" ", (name or "").lower()))
    return " ".join(cleaned.split())


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def match_application(msg: dict, apps: list) -> object | None:
    display, address = parseaddr(msg["from"])
    domain = address.split("@")[-1] if "@" in address else ""
    from_hay = norm_company(f"{display} {domain.replace('.', ' ').replace('-', ' ')}")
    subj_hay = norm_company(msg["subject"])
    snip_hay = norm_company(msg["snippet"])
    best, best_score = None, 0
    for app in apps:
        if app["thread_id"] and app["thread_id"] == msg["thread_id"]:
            return app
        company = norm_company(app["company"])
        if len(company) < 3:
            continue
        title = norm_company(app["title"])
        in_title = _contains(subj_hay, title) or _contains(snip_hay, title)
        score = 0
        if _contains(from_hay, company):
            score = 3
        elif _contains(subj_hay, company):
            score = 2
        elif _contains(snip_hay, company) and in_title:
            score = 2
        if score and in_title:
            score += 1
        if score > best_score:
            best, best_score = app, score
    return best


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply_transition(app_id: int, current: str, kind: str, at: str) -> str:
    """Move the application forward based on an email. Returns the resulting status."""
    new = EVENT_TO_STATUS[kind]
    fields = {}
    row = db.one("SELECT first_response_at FROM applications WHERE id=?", (app_id,))
    if kind != "ack" and row and not row["first_response_at"]:
        fields["first_response_at"] = at
    upgrade = STATUS_RANK[new] > STATUS_RANK[current]
    if current == "offer":
        upgrade = False
    if current == "rejected" and kind != "offer":
        upgrade = False
    if upgrade:
        db.set_status(app_id, new, **fields)
        return new
    if fields:
        db.execute("UPDATE applications SET first_response_at=? WHERE id=?", (fields["first_response_at"], app_id))
    return current


def reset_seen() -> None:
    """Forget which messages were already examined (so a rescan picks up new applications)."""
    db.execute("DELETE FROM seen_messages")


def poll_inbox(full: bool = False) -> dict:
    """Check the inbox. full=True forgets what was already examined and rescans the whole lookback window."""
    if not gmail.is_connected():
        return {"skipped": "Gmail is not connected"}
    if not _lock.acquire(blocking=False):
        return {"skipped": "an inbox check is already running, try again in a moment"}
    try:
        cfg = load_config()
        marks = ",".join("?" * len(SENT_STATUSES))
        apps = db.query(
            f"SELECT a.*, j.company, j.title FROM applications a JOIN jobs j ON j.id=a.job_id "
            f"WHERE a.status IN ({marks})", SENT_STATUSES)
        if full:
            reset_seen()
        if not apps:
            db.kv_set("last_inbox_check", db.now())
            db.kv_set("last_inbox_error", None)
            return {"tracking": 0, "checked": 0, "matched": 0}
        earliest = min((a["applied_at"] or a["created_at"]) for a in apps)
        days_back = (dt.datetime.now(dt.timezone.utc) -
                     dt.datetime.fromisoformat(earliest.replace("Z", "+00:00"))).days + 2
        lookback = int(cfg["gmail"]["poll_lookback_days"])
        # At least 14 days, because you may press "I applied" a few days after you really applied.
        days = lookback if full else max(14, min(lookback, days_back))

        ids = gmail.list_message_ids(f"newer_than:{days}d -from:me -in:chats -in:drafts")
        seen = {r["id"] for r in db.query("SELECT id FROM seen_messages")}
        fresh = [i for i in ids if i not in seen]

        messages = [gmail.get_message_meta(i) for i in fresh]
        messages.sort(key=lambda m: m["internal_ms"])
        matched = 0
        for msg in messages:
            db.execute("INSERT OR IGNORE INTO seen_messages (id, checked_at) VALUES (?, ?)", (msg["id"], db.now()))
            current_apps = db.query(
                f"SELECT a.*, j.company, j.title FROM applications a JOIN jobs j ON j.id=a.job_id "
                f"WHERE a.status IN ({marks})", SENT_STATUSES)
            app = match_application(msg, current_apps)
            if not app:
                continue
            kind, summary = classify(msg["subject"], msg["snippet"])
            at = _iso(msg["internal_ms"])
            detail = f"From: {msg['from']}\nSubject: {msg['subject']}\n\n{msg['snippet']}"
            if db.add_event(app["id"], kind, summary, detail, gmail_id=msg["id"], at=at):
                matched += 1
                apply_transition(app["id"], app["status"], kind, at)
        db.kv_set("last_inbox_check", db.now())
        db.kv_set("last_inbox_error", None)
        db.log("info", f"inbox: {len(fresh)} new messages checked, {matched} matched to {len(apps)} applications")
        return {"tracking": len(apps), "checked": len(fresh), "matched": matched}
    except Exception as exc:
        db.kv_set("last_inbox_error", f"{type(exc).__name__}: {exc}"[:300])
        db.log("error", f"inbox: {type(exc).__name__}: {exc}")
        return {"error": str(exc)}
    finally:
        _lock.release()


def mark_ghosted() -> int:
    """Applications with no human reply after N days become 'No reply'."""
    cfg = load_config()
    cutoff = (dt.datetime.now(dt.timezone.utc) -
              dt.timedelta(days=int(cfg["tracking"]["ghosted_after_days"]))).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = db.query(
        "SELECT id FROM applications WHERE status IN ('applied','acknowledged') "
        "AND applied_at IS NOT NULL AND applied_at < ? AND first_response_at IS NULL", (cutoff,))
    for row in rows:
        db.set_status(row["id"], "ghosted")
        db.add_event(row["id"], "ghosted", f"No reply after {cfg['tracking']['ghosted_after_days']} days")
    return len(rows)
