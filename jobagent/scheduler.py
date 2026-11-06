"""Background loop: search, apply, check the inbox, mark ghosted applications."""
from __future__ import annotations

import datetime as dt
import threading
import time

from . import db, gmail, pipeline, tracker
from .config import load_config

_thread: threading.Thread | None = None
_stop = threading.Event()


def _due(key: str, every_minutes: int) -> bool:
    last = db.kv_get(key)
    if not last:
        return True
    then = dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - then) >= dt.timedelta(minutes=every_minutes)


def tick() -> None:
    cfg = load_config()
    if _due("last_search", int(cfg["schedule"]["search_every_minutes"])):
        pipeline.run_search()
    if cfg["apply"]["mode"] != "off" and db.one("SELECT 1 FROM applications WHERE status='queued'"):
        pipeline.run_apply_queue()
    if gmail.is_connected() and _due("last_inbox_check", int(cfg["schedule"]["inbox_every_minutes"])):
        tracker.poll_inbox()
    if _due("last_ghost_check", 6 * 60):
        count = tracker.mark_ghosted()
        db.kv_set("last_ghost_check", db.now())
        if count:
            db.log("info", f"{count} application(s) marked as no reply")


def _loop() -> None:
    while not _stop.wait(60):
        try:
            tick()
        except Exception as exc:  # never let the loop die
            try:
                db.log("error", f"scheduler: {type(exc).__name__}: {exc}")
            except Exception:
                pass


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="jobagent-scheduler", daemon=True)
    _thread.start()
    # First pass shortly after start-up without blocking the web server.
    threading.Timer(5, lambda: _safe(tick)).start()


def _safe(fn) -> None:
    try:
        fn()
    except Exception as exc:
        db.log("error", f"scheduler: {type(exc).__name__}: {exc}")


def stop() -> None:
    _stop.set()
