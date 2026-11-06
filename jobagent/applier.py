"""Applying: email applications through Gmail, and form filling for Greenhouse / Lever
hosted application pages through a real browser (Playwright).

Deliberately NOT done: solving CAPTCHAs, getting around bot protection, logging in to
LinkedIn/Indeed on your behalf. When a page needs a human, the application is marked
"Apply manually" with a link, and you finish it in a normal browser.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from . import db, gmail
from .config import COVER_TEMPLATE_PATH, DATA_DIR, resume_path
from .matcher import matched_skills

DEFAULT_COVER_LETTER = """Dear {company} hiring team,

I am writing to apply for the {title} position. {skills_sentence}

I would welcome the chance to discuss how I can contribute to {company}. My CV is attached, and I am happy to share anything else that would help.

Kind regards,
{name}
{phone}
{email}
"""

FIELD_PATTERNS = [
    ("email", r"e-?mail"),
    ("first_name", r"first\s*name|given\s*name|forename"),
    ("last_name", r"last\s*name|surname|family\s*name"),
    ("full_name", r"^\W*(full\s*|legal\s*|your\s*)?name\b"),
    ("phone", r"phone|mobile|cell|telephone"),
    ("linkedin", r"linkedin"),
    ("github", r"github"),
    ("website", r"website|portfolio|personal\s*site|\burl\b|\blink\b"),
    ("cover_letter", r"cover\s*letter|motivation|why .*(interested|want|apply)|tell us about yourself"),
    ("current_company", r"current\s*(company|employer)|employer"),
    ("location", r"\bcity\b|current\s*location|where .*(based|located|live)|\blocation\b"),
]


def classify_field(label: str) -> str | None:
    """Map a form field's label/name text to a profile key (or None if unknown)."""
    text = (label or "").strip().lower()
    for key, pattern in FIELD_PATTERNS:
        if re.search(pattern, text):
            return key
    return None


def full_name(profile: dict) -> str:
    return f"{profile['first_name']} {profile['last_name']}".strip()


def build_cover_letter(cfg: dict, job) -> str:
    profile = cfg["profile"]
    template = COVER_TEMPLATE_PATH.read_text(encoding="utf-8") if COVER_TEMPLATE_PATH.exists() else DEFAULT_COVER_LETTER
    skills = matched_skills(job, cfg["search"])[:4]
    sentence = (f"My experience with {', '.join(skills[:-1]) + ' and ' + skills[-1] if len(skills) > 1 else skills[0]} "
                f"matches what the role asks for.") if skills else "The role matches the direction I am building my career in."

    class Safe(dict):
        def __missing__(self, key):
            return ""

    return template.format_map(Safe(
        company=job["company"], title=job["title"], skills_sentence=sentence, skills=", ".join(skills),
        name=full_name(profile), phone=profile["phone"], email=profile["email"],
        location=profile["location"], linkedin=profile["linkedin"], github=profile["github"],
    )).strip()


def target_kind(job) -> str:
    host = urlparse(job["apply_url"] or job["url"] or "").netloc.lower()
    if job["apply_email"]:
        return "email"
    if host.endswith("greenhouse.io"):
        return "greenhouse"
    if host.endswith("lever.co"):
        return "lever"
    return "manual"


def profile_ready(cfg: dict) -> str | None:
    p = cfg["profile"]
    if not (p["first_name"] and p["last_name"] and p["email"]):
        return "Fill in your name and email under profile: in Settings first."
    if not resume_path():
        return "Upload your CV in Settings first."
    return None


# ------------------------------------------------------------------- main entry

def apply(job, app, cfg: dict, submit: bool) -> dict:
    """Try to apply. Returns {status, method, error, screenshot, thread_id}.

    status is one of: applied | ready | manual | failed
    `submit=False` fills the form and takes a screenshot without sending it.
    """
    problem = profile_ready(cfg)
    if problem:
        return {"status": "manual", "method": "", "error": problem}
    kind = target_kind(job)
    cover = app["cover_letter"] or build_cover_letter(cfg, job)

    if kind == "email":
        return _apply_email(job, cfg, cover, submit)
    if kind in ("greenhouse", "lever"):
        return _apply_browser(job, app, cfg, cover, submit, kind)
    return {"status": "manual", "method": "link",
            "error": "This posting is on a site that can't be automated safely. Open it and apply there."}


def _apply_email(job, cfg: dict, cover: str, submit: bool) -> dict:
    if not submit:
        return {"status": "ready", "method": "email", "error": ""}
    if not gmail.is_connected() or not gmail.can_send():
        return {"status": "manual", "method": "email",
                "error": f"Email {job['apply_email']} yourself, or enable gmail.allow_send and reconnect Gmail."}
    try:
        sent = gmail.send_message(
            job["apply_email"], f"Application: {job['title']} - {full_name(cfg['profile'])}", cover, resume_path())
    except Exception as exc:
        return {"status": "failed", "method": "email", "error": str(exc)}
    return {"status": "applied", "method": "email", "error": "", "thread_id": sent.get("threadId", "")}


# ---------------------------------------------------------------------- browser

def _label_for(page, el) -> str:
    """Collect every piece of text that describes a form control."""
    parts = []
    for attr in ("aria-label", "placeholder", "name", "id"):
        value = el.get_attribute(attr)
        if value:
            parts.append(value)
    el_id = el.get_attribute("id")
    if el_id:
        label = page.query_selector(f'label[for="{el_id}"]')
        if label:
            parts.append(label.inner_text())
    wrapper = el.evaluate("e => (e.closest('label') || e.closest('.field') || e.closest('li') || e.parentElement || {}).innerText || ''")
    parts.append(wrapper[:160])
    return " ".join(parts)


def _display_label(page, el) -> str:
    """A short, human-readable name for a field, for messages shown to the user."""
    el_id = el.get_attribute("id")
    candidates = []
    if el_id:
        label = page.query_selector(f'label[for="{el_id}"]')
        if label:
            candidates.append(label.inner_text())
    candidates += [el.get_attribute("aria-label"), el.get_attribute("placeholder"),
                   el.evaluate("e => ((e.closest('label') || e.closest('.field') || e.parentElement || {}).innerText || '')"),
                   el.get_attribute("name"), el_id]
    for text in candidates:
        cleaned = re.sub(r"[\s*]+", " ", text or "").strip(" :")
        if cleaned:
            return cleaned[:60]
    return "unnamed field"


def _is_required(el, label_text: str) -> bool:
    return bool(el.get_attribute("required") is not None or el.get_attribute("aria-required") == "true"
                or "*" in label_text or "required" in label_text.lower())


def _apply_browser(job, app, cfg: dict, cover: str, submit: bool, kind: str) -> dict:
    try:
        from playwright.sync_api import Error as PWError
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"status": "manual", "method": kind,
                "error": "Playwright is not installed (pip install playwright && playwright install chromium)."}

    url = job["apply_url"] or job["url"]
    if kind == "lever" and not url.rstrip("/").endswith("/apply"):
        url = url.rstrip("/") + "/apply"
    profile = cfg["profile"]
    values = {
        "email": profile["email"], "first_name": profile["first_name"], "last_name": profile["last_name"],
        "full_name": full_name(profile), "phone": profile["phone"], "linkedin": profile["linkedin"],
        "github": profile["github"], "website": profile["website"] or profile["github"] or profile["linkedin"],
        "location": profile["location"], "cover_letter": cover, "current_company": profile["current_company"],
    }
    shot = DATA_DIR / "screenshots" / f"app_{app['id']}.png"
    unfilled: list[str] = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=bool(cfg["apply"]["headless"]))
            try:
                page = browser.new_context(viewport={"width": 1280, "height": 1600}).new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)

                if page.query_selector('iframe[src*="hcaptcha"]:visible, iframe[src*="recaptcha"][src*="bframe"]:visible, .h-captcha:visible'):
                    return {"status": "manual", "method": kind,
                            "error": "This form shows a CAPTCHA, which only a person should complete."}

                # Resume upload
                files = page.query_selector_all('input[type="file"]')
                target = next((f for f in files if re.search(r"resume|cv", _label_for(page, f), re.I)), files[0] if files else None)
                if target:
                    target.set_input_files(str(resume_path()))
                    page.wait_for_timeout(1500)

                # Text-like fields
                fields = page.query_selector_all(
                    'input:not([type="hidden"]):not([type="file"]):not([type="checkbox"]):not([type="radio"])'
                    ':not([type="submit"]):not([type="button"]), textarea')
                for el in fields:
                    if not el.is_visible() or not el.is_editable():
                        continue
                    label = _label_for(page, el)
                    key = classify_field(label)
                    if key and values.get(key):
                        el.fill(values[key])
                    elif el.input_value() == "" and _is_required(el, label):
                        unfilled.append(_display_label(page, el))

                # Required dropdowns / checkboxes we can't answer for the user
                for el in page.query_selector_all("select"):
                    if el.is_visible() and _is_required(el, _label_for(page, el)) and not el.input_value():
                        unfilled.append(_display_label(page, el))
                for el in page.query_selector_all('input[type="checkbox"]'):
                    if not el.is_visible():
                        continue
                    label = _label_for(page, el)
                    if re.search(r"privacy|data (processing|protection)|terms|consent", label, re.I) \
                            and cfg["apply"]["accept_privacy_checkboxes"]:
                        el.check()
                    elif _is_required(el, label) and not el.is_checked():
                        unfilled.append(_display_label(page, el))

                page.screenshot(path=str(shot), full_page=True)
                if unfilled:
                    names = ", ".join(sorted(set(unfilled))[:6])
                    return {"status": "manual", "method": kind, "screenshot": shot.name,
                            "error": f"The form asks questions I can't answer for you: {names}."}
                if not submit:
                    return {"status": "ready", "method": kind, "screenshot": shot.name, "error": ""}

                button = page.query_selector('button[type="submit"], input[type="submit"]')
                if not button:
                    return {"status": "manual", "method": kind, "screenshot": shot.name,
                            "error": "Could not find the submit button."}
                button.click()
                page.wait_for_timeout(6000)
                page.screenshot(path=str(shot), full_page=True)
                body = page.inner_text("body").lower()
                if re.search(r"thank you|application (has been )?(submitted|received)|successfully|we('ve| have) received", body) \
                        or re.search(r"confirmation|thanks|submitted", page.url, re.I):
                    return {"status": "applied", "method": kind, "screenshot": shot.name, "error": ""}
                return {"status": "manual", "method": kind, "screenshot": shot.name,
                        "error": "Could not confirm the submission. Check your inbox for a confirmation "
                                 "before trying again, to avoid applying twice."}
            finally:
                browser.close()
    except (PWTimeout, PWError) as exc:
        message = str(exc).splitlines()[0][:200]
        if "Executable doesn't exist" in str(exc):
            message = "Browser not installed: run `playwright install chromium`."
        return {"status": "failed", "method": kind, "error": message}
    except Exception as exc:
        return {"status": "failed", "method": kind, "error": f"{type(exc).__name__}: {exc}"[:200]}
