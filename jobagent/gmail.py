"""Minimal Gmail client: OAuth (installed-app flow) plus the few API calls we need.

Least privilege by default: read-only access. The send scope is only requested if
`gmail.allow_send` is true in the config. The refresh token is stored encrypted.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import time
import urllib.parse
from email.message import EmailMessage
from pathlib import Path

import requests

from . import crypto, db
from .config import DATA_DIR

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
BASE = "https://gmail.googleapis.com/gmail/v1/users/me/"
READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SEND = "https://www.googleapis.com/auth/gmail.send"

_cache = {"token": None, "exp": 0.0}


class GmailError(RuntimeError):
    pass


def find_client_file() -> Path | None:
    """The OAuth client file: gmail_client.json, or the name Google gives it (client_secret_....json).
    Also tolerates Windows hiding extensions, which can leave a file called gmail_client.json.json."""
    exact = DATA_DIR / "gmail_client.json"
    if exact.is_file():
        return exact
    for pattern in ("gmail_client*", "client_secret*"):
        for path in sorted(DATA_DIR.glob(pattern)):
            if path.is_file():
                return path
    return None


def _load_client() -> tuple[dict | None, str | None]:
    """Returns (client settings, None) or (None, plain-English reason it can't be used)."""
    path = find_client_file()
    if not path:
        names = sorted(p.name for p in DATA_DIR.iterdir() if p.is_file() and not p.name.startswith(".")) if DATA_DIR.is_dir() else []
        return None, (f"No client file found. The app is looking in {DATA_DIR} (files there: {', '.join(names) or 'none'}). "
                      "Save the JSON you downloaded from Google into that folder, named gmail_client.json.")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))  # utf-8-sig: tolerate a Windows byte-order mark
    except (OSError, ValueError) as exc:
        return None, f"{path.name} could not be read as JSON ({exc}). Download the OAuth client JSON from Google again."
    block = (data.get("installed") or data.get("web")) if isinstance(data, dict) else None
    if not isinstance(block, dict):
        keys = ", ".join(map(str, data.keys())) if isinstance(data, dict) else type(data).__name__
        hint = (" That is a service-account key. You need Credentials > Create credentials > OAuth client ID > Desktop app."
                if isinstance(data, dict) and data.get("type") == "service_account" else "")
        return None, f"{path.name} is not an OAuth client file (it contains: {keys}).{hint}"
    if not block.get("client_id") or not block.get("client_secret"):
        return None, (f"{path.name} has no client_id or client_secret. Google only shows a new client's secret once: "
                      "create a new Desktop-app client and download its JSON straight away.")
    return block, None


def _client() -> dict | None:
    return _load_client()[0]


def client_problem() -> str | None:
    """None when the client file is usable, otherwise the reason it isn't."""
    return _load_client()[1]


def is_configured() -> bool:
    return client_problem() is None


def is_connected() -> bool:
    return bool(db.kv_get("gmail_refresh"))


def account_email() -> str:
    return db.kv_get("gmail_email", "") or ""


def can_send() -> bool:
    return SEND in (db.kv_get("gmail_scopes", "") or "")


def auth_url(redirect_uri: str, state: str, allow_send: bool) -> str:
    client = _client()
    if not client:
        raise GmailError(client_problem() or "Gmail client file is missing")
    scopes = [READONLY] + ([SEND] if allow_send else [])
    params = {
        "client_id": client["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(scopes), "access_type": "offline", "prompt": "consent", "state": state,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(code: str, redirect_uri: str) -> None:
    client = _client()
    response = requests.post(TOKEN_URL, data={
        "code": code, "client_id": client["client_id"], "client_secret": client["client_secret"],
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }, timeout=30)
    if response.status_code != 200:
        raise GmailError(f"Google rejected the sign-in: {response.text[:200]}")
    token = response.json()
    if not token.get("refresh_token"):
        raise GmailError("Google did not return a refresh token; remove the app at "
                         "myaccount.google.com/permissions and connect again.")
    granted = token.get("scope", "")
    if READONLY not in granted:
        # Google lets people untick individual permissions on the consent screen.
        raise GmailError("Google signed you in, but the permission to read your email was not granted. Connect again and, "
                         "on the permissions screen, tick the box that lets Job Agent view your email messages "
                         "before pressing Continue.")
    db.kv_set("gmail_refresh", crypto.encrypt(token["refresh_token"]))
    db.kv_set("gmail_scopes", granted)
    _cache.update(token=token["access_token"], exp=time.time() + int(token.get("expires_in", 3600)) - 60)
    try:
        profile = api("GET", "profile")
    except Exception as exc:
        disconnect()
        raise GmailError(f"Signed in, but Gmail would not answer: {exc}. Check that the Gmail API is enabled in the SAME "
                         "Google Cloud project that your OAuth client belongs to.") from exc
    db.kv_set("gmail_email", profile.get("emailAddress", ""))


def disconnect() -> None:
    refresh = crypto.decrypt(db.kv_get("gmail_refresh"))
    if refresh:
        try:
            requests.post(REVOKE_URL, params={"token": refresh}, timeout=15)
        except requests.RequestException:
            pass
    for key in ("gmail_refresh", "gmail_scopes", "gmail_email"):
        db.kv_set(key, None)
    _cache.update(token=None, exp=0.0)


def _access_token() -> str:
    if _cache["token"] and time.time() < _cache["exp"]:
        return _cache["token"]
    refresh = crypto.decrypt(db.kv_get("gmail_refresh"))
    client = _client()
    if not refresh or not client:
        raise GmailError("Gmail is not connected")
    response = requests.post(TOKEN_URL, data={
        "client_id": client["client_id"], "client_secret": client["client_secret"],
        "refresh_token": refresh, "grant_type": "refresh_token",
    }, timeout=30)
    if response.status_code != 200:
        raise GmailError(f"Could not refresh the Gmail token ({response.status_code}); reconnect Gmail in Settings.")
    token = response.json()
    _cache.update(token=token["access_token"], exp=time.time() + int(token.get("expires_in", 3600)) - 60)
    return _cache["token"]


def api(method: str, path: str, **kwargs) -> dict:
    for attempt in (1, 2):
        headers = {"Authorization": f"Bearer {_access_token()}"}
        response = requests.request(method, BASE + path, headers=headers, timeout=30, **kwargs)
        if response.status_code == 401 and attempt == 1:
            _cache.update(token=None, exp=0.0)
            continue
        if response.status_code >= 400:
            raise GmailError(f"Gmail API {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else {}
    raise GmailError("Gmail API: unauthorized")


def list_message_ids(query: str, max_pages: int = 3) -> list[str]:
    ids: list[str] = []
    token = None
    for _ in range(max_pages):
        params = {"q": query, "maxResults": 100}
        if token:
            params["pageToken"] = token
        data = api("GET", "messages", params=params)
        ids.extend(m["id"] for m in data.get("messages", []))
        token = data.get("nextPageToken")
        if not token:
            break
    return ids


def get_message_meta(message_id: str) -> dict:
    """Headers + snippet only: the app never downloads full message bodies."""
    data = api("GET", f"messages/{message_id}", params={
        "format": "metadata", "metadataHeaders": ["From", "Subject", "Date"],
    })
    headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
    return {
        "id": data["id"], "thread_id": data.get("threadId", ""),
        "from": headers.get("from", ""), "subject": headers.get("subject", ""),
        "snippet": data.get("snippet", ""), "internal_ms": int(data.get("internalDate", 0)),
    }


def send_message(to: str, subject: str, body: str, attachment: Path | None = None) -> dict:
    if not can_send():
        raise GmailError("Sending is not enabled: set gmail.allow_send: true and reconnect Gmail.")
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if attachment and attachment.exists():
        ctype = mimetypes.guess_type(attachment.name)[0] or "application/octet-stream"
        main, sub = ctype.split("/", 1)
        msg.add_attachment(attachment.read_bytes(), maintype=main, subtype=sub, filename=attachment.name)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return api("POST", "messages/send", json={"raw": raw})
