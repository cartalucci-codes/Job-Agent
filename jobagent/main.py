"""The web dashboard (Starlette). Meant to run on your own computer only."""
from __future__ import annotations

import contextlib
import datetime as dt
import re
import secrets
import threading
import uuid
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import applier, config, db, gmail, pipeline, scheduler, security, stats, tracker
from .constants import ACTIVE_STATUSES, EVENT_LABELS, STATUS_LABELS

PKG = config.PKG_DIR


# ------------------------------------------------------------------- templating

def _tz():
    try:
        return ZoneInfo(config.load_config()["timezone"])
    except Exception:
        return dt.timezone.utc


def _parse(value: str | None):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def f_localtime(value, fmt="%a %d %b, %H:%M"):
    parsed = _parse(value)
    return parsed.astimezone(_tz()).strftime(fmt) if parsed else "-"


def f_localdate(value):
    return f_localtime(value, "%d %b %Y")


def f_localinput(value):
    return f_localtime(value, "%Y-%m-%dT%H:%M") if value else ""


def f_ago(value):
    parsed = _parse(value)
    if not parsed:
        return "-"
    seconds = int((dt.datetime.now(dt.timezone.utc) - parsed).total_seconds())
    future = seconds < 0
    seconds = abs(seconds)
    for limit, unit, size in ((3600, "m", 60), (86400, "h", 3600), (86400 * 60, "d", 86400)):
        if seconds < limit:
            text = f"{max(1, seconds // size)}{unit}"
            break
    else:
        text = f"{seconds // (86400 * 30)}mo"
    return f"in {text}" if future else f"{text} ago"


def _context(request: Request) -> dict:
    nav = [("/", "Dashboard"), ("/review", "Review"), ("/applications", "Applications"),
           ("/interviews", "Interviews"), ("/jobs", "Jobs"), ("/settings", "Settings")]
    review = db.one("SELECT COUNT(*) n FROM applications WHERE status='review'")["n"]
    return {"nav": nav, "review_count": review, "flash": request.query_params.get("msg", "")}


templates = Jinja2Templates(directory=str(PKG / "templates"), context_processors=[_context])
templates.env.filters.update(localtime=f_localtime, localdate=f_localdate, localinput=f_localinput, ago=f_ago)
templates.env.globals.update(labels=STATUS_LABELS, event_labels=EVENT_LABELS)


def render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, ctx)


def back(to: str, msg: str | None = None):
    if msg:
        to += ("&" if "?" in to else "?") + "msg=" + quote(msg)
    return RedirectResponse(to, status_code=303)


def bg(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


# --------------------------------------------------------------------- security

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/static") or path == "/login":
            return await call_next(request)
        if not security.is_authed(request.cookies.get(security.COOKIE)):
            return RedirectResponse("/login", status_code=303)
        if request.method == "POST":  # block cross-site form posts
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            if origin and urlparse(origin).netloc != request.headers.get("host", ""):
                return PlainTextResponse("Cross-origin request blocked.", status_code=403)
        return await call_next(request)


async def login(request: Request):
    if request.method == "POST":
        form = await request.form()
        if security.check_token(str(form.get("token", ""))):
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(security.COOKIE, security.session_value(), httponly=True, samesite="lax",
                                max_age=60 * 60 * 24 * 30)
            return response
        import asyncio
        await asyncio.sleep(1.0)
        return render(request, "login.html", error="That token is not right.")
    return render(request, "login.html", error="")


async def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(security.COOKIE)
    return response


# -------------------------------------------------------------------- dashboard

async def dashboard(request: Request):
    upcoming = db.query(
        "SELECT i.*, a.id app_id, j.company, j.title FROM interviews i JOIN applications a ON a.id=i.application_id "
        "JOIN jobs j ON j.id=a.job_id WHERE i.scheduled_at >= ? AND i.outcome='pending' ORDER BY i.scheduled_at LIMIT 6",
        ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    recent = db.query(
        "SELECT e.*, a.id app_id, j.company, j.title FROM events e JOIN applications a ON a.id=e.application_id "
        "JOIN jobs j ON j.id=a.job_id WHERE e.kind != 'found' ORDER BY e.at DESC LIMIT 14")
    heard = db.query(LIST_SQL + " WHERE a.status IN ('replied','interviewing','offer','rejected') "
                     "ORDER BY a.first_response_at DESC LIMIT 6")
    waiting = db.query(LIST_SQL + " WHERE a.status IN ('applied','acknowledged','ghosted') "
                       "ORDER BY a.applied_at ASC LIMIT 6")
    return render(request, "dashboard.html", s=stats.overview(), weeks=stats.weekly_applied(),
                  attention=stats.attention(), upcoming=upcoming, recent=recent, heard=heard, waiting=waiting,
                  last_search=db.kv_get("last_search"), last_inbox=db.kv_get("last_inbox_check"),
                  inbox_error=db.kv_get("last_inbox_error"), tracking=tracking_count(),
                  gmail_connected=gmail.is_connected(), cfg=config.load_config())


async def run_search_now(request: Request):
    bg(pipeline.run_search)
    return back("/", "Search started: new matches will appear in Review shortly.")


def tracking_count() -> int:
    return db.one("SELECT COUNT(*) n FROM applications WHERE status IN "
                  "('applied','acknowledged','replied','interviewing','ghosted')")["n"]


def inbox_message(result: dict) -> str:
    """Turn an inbox-check result into a sentence a person can act on."""
    if result.get("skipped"):
        return result["skipped"] + "."
    if result.get("error"):
        return "Gmail problem: " + result["error"]
    if not result.get("tracking"):
        return ("Gmail is connected and working, but there are no applications to track yet. Press 'I applied' on a job "
                "in Review (or add one under Applications), then check again.")
    text = (f"Checked {result['checked']} new email(s); {result['matched']} matched your "
            f"{result['tracking']} tracked application(s).")
    if not result["matched"]:
        text += " No replies from those companies yet."
    return text


async def run_inbox_now(request: Request):
    if not gmail.is_connected():
        return back("/settings", "Connect Gmail first so replies can be tracked.")
    form = await request.form()
    result = await run_in_threadpool(tracker.poll_inbox, form.get("full") == "1")
    return back("/settings" if form.get("next") == "/settings" else "/", inbox_message(result))


# ----------------------------------------------------------------------- review

async def review(request: Request):
    rows = db.query(
        "SELECT a.id app_id, a.status, j.* FROM applications a JOIN jobs j ON j.id=a.job_id "
        "WHERE a.status='review' ORDER BY j.score DESC, j.found_at DESC")
    return render(request, "review.html", rows=rows, cfg=config.load_config(), kind=applier.target_kind)


async def review_action(request: Request):
    app_id = request.path_params["id"]
    action = request.path_params["action"]
    row = db.one("SELECT status FROM applications WHERE id=?", (app_id,))
    if not row:
        return back("/review", "That application no longer exists.")
    if action == "approve":
        db.set_status(app_id, "queued")
        db.add_event(app_id, "approved", "You approved this application")
        bg(pipeline.run_apply_queue)
        return back("/review", "Approved: it will be processed now.")
    db.set_status(app_id, "dismissed")
    db.add_event(app_id, "status", "You dismissed this job")
    return back("/review", "Dismissed.")


async def review_approve_all(request: Request):
    ids = [r["id"] for r in db.query("SELECT id FROM applications WHERE status='review'")]
    for app_id in ids:
        db.set_status(app_id, "queued")
        db.add_event(app_id, "approved", "You approved this application")
    bg(pipeline.run_apply_queue)
    return back("/review", f"Approved {len(ids)} application(s).")


# ----------------------------------------------------------------- applications

LIST_SQL = ("SELECT a.*, j.company, j.title, j.score, j.url, j.source FROM applications a "
            "JOIN jobs j ON j.id=a.job_id")


async def applications(request: Request):
    status = request.query_params.get("status", "")
    q = request.query_params.get("q", "").strip()
    sql, params = LIST_SQL + " WHERE 1=1", []
    groups = {"heard": ("replied", "interviewing", "offer", "rejected"),
              "waiting": ("applied", "acknowledged", "ghosted")}
    if status in groups:
        sql += f" AND a.status IN ({','.join('?' * len(groups[status]))})"
        params += list(groups[status])
    elif status:
        sql += " AND a.status=?"
        params.append(status)
    if q:
        sql += " AND (j.company LIKE ? OR j.title LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY COALESCE(a.last_event_at, a.created_at) DESC LIMIT 400"
    counts = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM applications GROUP BY status")}
    return render(request, "applications.html", rows=db.query(sql, tuple(params)), counts=counts,
                  status=status, q=q, today=dt.date.today().isoformat())


async def application_add(request: Request):
    form = await request.form()
    company, title = str(form.get("company", "")).strip(), str(form.get("title", "")).strip()
    if not company or not title:
        return back("/applications", "Company and job title are both required.")
    status = str(form.get("status", "applied"))
    if status not in STATUS_LABELS:
        status = "applied"
    applied_on = str(form.get("applied_on", "")) or dt.date.today().isoformat()
    applied_at = applied_on + "T12:00:00Z"
    url = str(form.get("url", "")).strip()
    job_id = db.execute(
        "INSERT INTO jobs (source, external_id, title, company, url, apply_url, found_at, status, dedupe_key, score) "
        "VALUES ('manual', ?, ?, ?, ?, ?, ?, 'matched', ?, 0)",
        (uuid.uuid4().hex, title, company, url, url, db.now(), pipeline.dedupe_key(company, title)))
    app_id = db.execute(
        "INSERT INTO applications (job_id, status, method, created_at, applied_at, last_event_at, notes) "
        "VALUES (?,?,?,?,?,?,?)",
        (job_id, status, "manual", db.now(), applied_at, applied_at, str(form.get("notes", ""))))
    db.add_event(app_id, "applied", "Added manually", at=applied_at)
    tracker.reset_seen()
    return back(f"/applications/{app_id}", "Added. Replies from this company will be matched from your inbox.")


async def application_detail(request: Request):
    app_id = request.path_params["id"]
    row = db.one(LIST_SQL.replace("j.source", "j.source, j.description, j.reasons, j.location, j.apply_url, j.apply_email")
                 + " WHERE a.id=?", (app_id,))
    if not row:
        return PlainTextResponse("Not found", status_code=404)
    events = db.query("SELECT * FROM events WHERE application_id=? ORDER BY at DESC, id DESC", (app_id,))
    interviews = db.query("SELECT * FROM interviews WHERE application_id=? ORDER BY scheduled_at", (app_id,))
    cfg = config.load_config()
    return render(request, "application.html", a=row, events=events, interviews=interviews,
                  target=applier.target_kind(row), dry_run=cfg["apply"]["dry_run"], mode=cfg["apply"]["mode"])


async def application_update(request: Request):
    app_id = request.path_params["id"]
    what = request.path_params["what"]
    form = await request.form()
    row = db.one("SELECT * FROM applications WHERE id=?", (app_id,))
    if not row:
        return back("/applications", "Not found.")
    here = f"/applications/{app_id}"

    if what == "status":
        status = str(form.get("status", ""))
        if status not in STATUS_LABELS:
            return back(here, "Unknown status.")
        fields = {}
        if status in ("applied", "acknowledged", "replied", "interviewing", "offer", "rejected") and not row["applied_at"]:
            fields["applied_at"] = db.now()
        db.set_status(app_id, status, **fields)
        db.add_event(app_id, "status", f"You set the status to {STATUS_LABELS[status]}")
        if status in ("applied",):
            tracker.reset_seen()
        return back(here, "Status updated.")
    if what == "applied":  # you applied yourself on the company's site
        db.set_status(app_id, "applied", applied_at=row["applied_at"] or db.now(), method=row["method"] or "manual")
        db.add_event(app_id, "applied", "You applied manually")
        tracker.reset_seen()
        target = str(form.get("next", ""))
        dest = target if target.startswith("/") and not target.startswith("//") else here
        return back(dest, "Marked as applied. Replies will be matched from your inbox.")
    if what == "notes":
        db.execute("UPDATE applications SET notes=? WHERE id=?", (str(form.get("notes", "")), app_id))
        return back(here, "Notes saved.")
    if what == "cover":
        db.execute("UPDATE applications SET cover_letter=? WHERE id=?", (str(form.get("cover_letter", "")), app_id))
        return back(here, "Cover letter saved.")
    if what == "submit":
        db.set_status(app_id, "queued")
        bg(pipeline.apply_one, app_id, True)
        return back(here, "Submitting in the background. Refresh in a moment.")
    if what == "prepare":
        db.set_status(app_id, "queued")
        bg(pipeline.apply_one, app_id, False)
        return back(here, "Preparing the form in the background. Refresh in a moment.")
    if what == "delete":
        job = db.one("SELECT job_id FROM applications WHERE id=?", (app_id,))
        db.execute("DELETE FROM jobs WHERE id=?", (job["job_id"],))
        return back("/applications", "Deleted.")
    return back(here)


async def interview_add(request: Request):
    app_id = request.path_params["id"]
    form = await request.form()
    when = str(form.get("scheduled_at", "")).strip()
    scheduled = None
    if when:
        try:
            local = dt.datetime.strptime(when, "%Y-%m-%dT%H:%M").replace(tzinfo=_tz())
            scheduled = local.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return back(f"/applications/{app_id}", "That date/time was not understood.")
    db.execute(
        "INSERT INTO interviews (application_id, scheduled_at, kind, link, interviewer, notes) VALUES (?,?,?,?,?,?)",
        (app_id, scheduled, str(form.get("kind", "Interview")) or "Interview", str(form.get("link", "")),
         str(form.get("interviewer", "")), str(form.get("notes", ""))))
    row = db.one("SELECT status FROM applications WHERE id=?", (app_id,))
    if row and row["status"] in ("applied", "acknowledged", "replied", "ghosted"):
        db.set_status(app_id, "interviewing")
    db.add_event(app_id, "interview", f"{form.get('kind') or 'Interview'} scheduled" + (f" for {f_localtime(scheduled)}" if scheduled else ""))
    return back(f"/applications/{app_id}", "Interview added.")


async def interview_update(request: Request):
    interview_id = request.path_params["id"]
    action = request.path_params["action"]
    row = db.one("SELECT * FROM interviews WHERE id=?", (interview_id,))
    if not row:
        return back("/interviews", "Not found.")
    if action == "delete":
        db.execute("DELETE FROM interviews WHERE id=?", (interview_id,))
    else:
        form = await request.form()
        outcome = str(form.get("outcome", "pending"))
        db.execute("UPDATE interviews SET outcome=?, notes=? WHERE id=?",
                   (outcome, str(form.get("notes", row["notes"])), interview_id))
        if outcome != row["outcome"] and outcome != "pending":
            db.add_event(row["application_id"], "interview", f"{row['kind']} outcome: {outcome}")
    return back(str(request.query_params.get("next", "/interviews")))


async def interviews(request: Request):
    base = ("SELECT i.*, a.id app_id, j.company, j.title FROM interviews i JOIN applications a ON a.id=i.application_id "
            "JOIN jobs j ON j.id=a.job_id ")
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    upcoming = db.query(base + "WHERE (i.scheduled_at >= ? OR i.scheduled_at IS NULL) AND i.outcome='pending' "
                        "ORDER BY i.scheduled_at IS NULL, i.scheduled_at", (cutoff,))
    past = db.query(base + "WHERE NOT ((i.scheduled_at >= ? OR i.scheduled_at IS NULL) AND i.outcome='pending') "
                    "ORDER BY i.scheduled_at DESC LIMIT 100", (cutoff,))
    invites = db.query(
        "SELECT a.id, j.company, j.title FROM applications a JOIN jobs j ON j.id=a.job_id "
        "WHERE a.status='interviewing' AND NOT EXISTS (SELECT 1 FROM interviews i WHERE i.application_id=a.id)")
    return render(request, "interviews.html", upcoming=upcoming, past=past, invites=invites)


async def event_delete(request: Request):
    event = db.one("SELECT application_id FROM events WHERE id=?", (request.path_params["id"],))
    if event:
        db.execute("DELETE FROM events WHERE id=?", (request.path_params["id"],))
        return back(f"/applications/{event['application_id']}", "Event removed.")
    return back("/applications")


# ------------------------------------------------------------------------- jobs

async def jobs(request: Request):
    status = request.query_params.get("status", "")
    q = request.query_params.get("q", "").strip()
    sql, params = "SELECT * FROM jobs WHERE source != 'manual'", []
    if status:
        sql += " AND status=?"
        params.append(status)
    if q:
        sql += " AND (company LIKE ? OR title LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY found_at DESC, score DESC LIMIT 300"
    counts = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM jobs WHERE source != 'manual' GROUP BY status")}
    return render(request, "jobs.html", rows=db.query(sql, tuple(params)), counts=counts, status=status, q=q,
                  min_score=config.load_config()["search"]["min_score"])


async def job_queue(request: Request):
    cfg = config.load_config()
    app_id = pipeline.queue_job(request.path_params["id"], cfg, "review")
    return back("/review" if app_id else "/jobs", "Added to your review list." if app_id else "Already queued.")


# --------------------------------------------------------------------- settings

async def settings(request: Request):
    cfg = config.load_config()
    resume = config.resume_path()
    return render(
        request, "settings.html", config_text=config.config_text(), cfg=cfg,
        resume=resume.name if resume else None,
        cover_template=config.COVER_TEMPLATE_PATH.read_text(encoding="utf-8") if config.COVER_TEMPLATE_PATH.exists() else applier.DEFAULT_COVER_LETTER,
        gmail_configured=gmail.is_configured(), gmail_problem=gmail.client_problem(), data_dir=str(config.DATA_DIR),
        gmail_connected=gmail.is_connected(),
        gmail_email=gmail.account_email(), gmail_can_send=gmail.can_send(),
        logs=db.query("SELECT * FROM logs ORDER BY id DESC LIMIT 30"),
        last_search=db.kv_get("last_search"), last_inbox=db.kv_get("last_inbox_check"),
        inbox_error=db.kv_get("last_inbox_error"), tracking=tracking_count(),
        ready_problem=applier.profile_ready(cfg))


async def settings_config(request: Request):
    form = await request.form()
    text = str(form.get("config", ""))
    error = config.save_config_text(text)
    if error:
        return back("/settings", error)
    changed = pipeline.rescore_all()
    return back("/settings", "Saved." + (f" {changed} previously skipped job(s) now qualify." if changed else ""))


async def settings_resume(request: Request):
    form = await request.form()
    upload = form.get("resume")
    if not upload or not getattr(upload, "filename", ""):
        return back("/settings", "Choose a file first.")
    ext = upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
    if ext not in ("pdf", "docx", "doc"):
        return back("/settings", "The CV must be a PDF or Word file.")
    data = await upload.read()
    if len(data) > 5 * 1024 * 1024:
        return back("/settings", "That file is over 5 MB.")
    for old in ("pdf", "docx", "doc"):
        with contextlib.suppress(FileNotFoundError):
            (config.DATA_DIR / f"resume.{old}").unlink()
    target = config.DATA_DIR / f"resume.{ext}"
    target.write_bytes(data)
    target.chmod(0o600)
    return back("/settings", "CV saved.")


async def settings_cover(request: Request):
    form = await request.form()
    text = str(form.get("cover", "")).strip()
    if text:
        config.COVER_TEMPLATE_PATH.write_text(text, encoding="utf-8")
    elif config.COVER_TEMPLATE_PATH.exists():
        config.COVER_TEMPLATE_PATH.unlink()
    return back("/settings", "Cover letter template saved. It applies to jobs queued from now on.")


# ------------------------------------------------------------------------ gmail

def _redirect_uri(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers['host']}/gmail/callback"


async def gmail_connect(request: Request):
    problem = gmail.client_problem()
    if problem:
        return back("/settings", problem)
    state = secrets.token_urlsafe(16)
    db.kv_set("oauth_state", state)
    url = gmail.auth_url(_redirect_uri(request), state, bool(config.load_config()["gmail"]["allow_send"]))
    return RedirectResponse(url, status_code=303)


async def gmail_callback(request: Request):
    params = request.query_params
    if params.get("error"):
        return back("/settings", f"Google said: {params['error']}")
    expected = db.kv_get("oauth_state")
    db.kv_set("oauth_state", None)
    if not expected or not secrets.compare_digest(expected, params.get("state", "")):
        return back("/settings", "Sign-in check failed. Please try connecting again.")
    try:
        gmail.exchange_code(params["code"], _redirect_uri(request))
    except Exception as exc:
        return back("/settings", str(exc))
    result = await run_in_threadpool(tracker.poll_inbox)
    return back("/settings", "Gmail connected. " + inbox_message(result))


async def gmail_disconnect(request: Request):
    gmail.disconnect()
    return back("/settings", "Gmail disconnected.")


async def screenshot(request: Request):
    name = request.path_params["name"]
    path = config.DATA_DIR / "screenshots" / name
    if not re.fullmatch(r"app_\d+\.png", name) or not path.exists():
        return PlainTextResponse("Not found", status_code=404)
    return FileResponse(path)


# -------------------------------------------------------------------------- app

@contextlib.asynccontextmanager
async def lifespan(app):
    db.init()
    print(f"\n  Job Agent is running. Your access token: {security.access_token()}")
    print(f"  Data folder: {config.DATA_DIR}\n")
    scheduler.start()
    yield
    scheduler.stop()


routes = [
    Route("/login", login, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/", dashboard),
    Route("/run/search", run_search_now, methods=["POST"]),
    Route("/run/inbox", run_inbox_now, methods=["POST"]),
    Route("/review", review),
    Route("/review/approve_all", review_approve_all, methods=["POST"]),
    Route("/review/{id:int}/{action}", review_action, methods=["POST"]),
    Route("/applications", applications),
    Route("/applications/manual", application_add, methods=["POST"]),
    Route("/applications/{id:int}", application_detail),
    Route("/applications/{id:int}/interviews", interview_add, methods=["POST"]),
    Route("/applications/{id:int}/{what}", application_update, methods=["POST"]),
    Route("/interviews", interviews),
    Route("/interviews/{id:int}/{action}", interview_update, methods=["POST"]),
    Route("/events/{id:int}/delete", event_delete, methods=["POST"]),
    Route("/jobs", jobs),
    Route("/jobs/{id:int}/queue", job_queue, methods=["POST"]),
    Route("/settings", settings),
    Route("/settings/config", settings_config, methods=["POST"]),
    Route("/settings/resume", settings_resume, methods=["POST"]),
    Route("/settings/cover", settings_cover, methods=["POST"]),
    Route("/gmail/connect", gmail_connect, methods=["POST"]),
    Route("/gmail/callback", gmail_callback),
    Route("/gmail/disconnect", gmail_disconnect, methods=["POST"]),
    Route("/screenshots/{name}", screenshot),
    Mount("/static", StaticFiles(directory=str(PKG / "static")), name="static"),
]

app = Starlette(routes=routes, middleware=[Middleware(AuthMiddleware)], lifespan=lifespan)
