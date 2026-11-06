"""Job sources. Only official, public job-board APIs and feeds are used."""
from __future__ import annotations

import datetime as dt
import email.utils
import html
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from . import db
from .matcher import _has

UA = "JobAgent/0.1 (personal job-search tool)"
TIMEOUT = 25
PAUSE = 1.0  # seconds between requests to the same service
MAX_DETAIL_CALLS = 25  # SmartRecruiters: extra requests per company for job descriptions


@dataclass
class Job:
    source: str
    external_id: str
    title: str
    company: str
    location: str = ""
    remote: bool = False
    url: str = ""
    apply_url: str = ""
    apply_email: str = ""
    description: str = ""
    posted_at: str = ""


def _get(url: str, params: dict | None = None) -> requests.Response:
    response = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=TIMEOUT)
    response.raise_for_status()
    time.sleep(PAUSE)
    return response


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    if "&lt;" in raw and "<" not in raw:
        raw = html.unescape(raw)
    return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)


def to_iso(value) -> str:
    if not value:
        return ""
    try:
        if isinstance(value, (int, float)):
            if value > 1e12:
                value /= 1000
            stamp = dt.datetime.fromtimestamp(value, dt.timezone.utc)
        else:
            text = str(value).strip()
            if text.isdigit():
                return to_iso(int(text))
            try:
                stamp = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                stamp = email.utils.parsedate_to_datetime(text)  # RSS dates
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=dt.timezone.utc)
        return stamp.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError, OSError, OverflowError):
        return ""


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_APPLY_LOCALS = {"jobs", "job", "careers", "career", "hr", "recruitment", "recruiting",
                 "apply", "applications", "talent", "hiring", "vacancies", "cv"}


def find_apply_email(text: str) -> str:
    for match in _EMAIL.findall(text or ""):
        if match.split("@")[0].lower() in _APPLY_LOCALS:
            return match
    return ""


def pretty(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").title()


def _title_plausible(title: str, search: dict) -> bool:
    """Cheap title check, used to avoid fetching details for jobs that could never match."""
    low = (title or "").lower()
    if any(_has(bad, low) for bad in search["exclude_title"]):
        return False
    for wanted in search["titles"]:
        words = [w for w in re.split(r"\W+", wanted.lower()) if w]
        if words and sum(1 for w in words if _has(w, low)) / len(words) >= 0.5:
            return True
    return False


def _per_company(source: str, names: list[str], fn) -> list[Job]:
    """Run fn(name) for each company; a bad name never stops the others."""
    jobs: list[Job] = []
    for name in names:
        try:
            jobs.extend(fn(str(name).strip()))
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            hint = " (check the company name/token spelling)" if code == 404 else ""
            db.log("error", f"{source}/{name}: HTTP {code}{hint}")
        except Exception as exc:
            db.log("error", f"{source}/{name}: {type(exc).__name__}: {exc}")
    return jobs


# --------------------------------------------------------------- job boards

def fetch_remotive(cfg: dict) -> list[Job]:
    jobs: dict[str, Job] = {}
    for title in cfg["search"]["titles"][:6]:
        data = _get("https://remotive.com/api/remote-jobs", {"search": title, "limit": 50}).json()
        for item in data.get("jobs", []):
            ext = str(item.get("id"))
            text = html_to_text(item.get("description", ""))
            jobs[ext] = Job(
                source="remotive", external_id=ext, title=item.get("title", ""),
                company=item.get("company_name", ""), location=item.get("candidate_required_location", "") or "Worldwide",
                remote=True, url=item.get("url", ""), apply_url=item.get("url", ""),
                apply_email=find_apply_email(text), description=text, posted_at=to_iso(item.get("publication_date")),
            )
    return list(jobs.values())


def fetch_arbeitnow(cfg: dict) -> list[Job]:
    jobs = []
    for page in (1, 2, 3):
        data = _get("https://www.arbeitnow.com/api/job-board-api", {"page": page}).json()
        for item in data.get("data", []):
            text = html_to_text(item.get("description", ""))
            jobs.append(Job(
                source="arbeitnow", external_id=str(item.get("slug")), title=item.get("title", ""),
                company=item.get("company_name", ""), location=item.get("location", ""),
                remote=bool(item.get("remote")), url=item.get("url", ""), apply_url=item.get("url", ""),
                apply_email=find_apply_email(text), description=text, posted_at=to_iso(item.get("created_at")),
            ))
    return jobs


def fetch_himalayas(cfg: dict) -> list[Job]:
    jobs, offset = [], 0
    for _ in range(5):
        data = _get("https://himalayas.app/jobs/api", {"limit": 100, "offset": offset}).json()
        items = data.get("jobs", [])
        if not items:
            break
        offset += len(items)
        for item in items:
            restrictions = item.get("locationRestrictions") or []
            company = item.get("companyName", "")
            title = item.get("title", "")
            link = item.get("applicationLink", "") or ""
            guid = item.get("guid", "") or ""
            text = html_to_text(item.get("description") or item.get("excerpt") or "")
            jobs.append(Job(
                source="himalayas", external_id=guid or f"{company}:{title}", title=title, company=company,
                location=", ".join(restrictions) if restrictions else "Worldwide", remote=True,
                url=guid if guid.startswith("http") else link, apply_url=link or guid,
                apply_email=find_apply_email(text), description=text, posted_at=to_iso(item.get("pubDate")),
            ))
    return jobs


def fetch_remoteok(cfg: dict) -> list[Job]:
    jobs = []
    for item in _get("https://remoteok.com/api").json():
        if not isinstance(item, dict) or not item.get("position"):
            continue  # the first element of the feed is a legal notice
        tags = " ".join(item.get("tags") or [])
        text = html_to_text(item.get("description", "")) + " " + tags
        url = item.get("url") or item.get("apply_url") or ""
        jobs.append(Job(
            source="remoteok", external_id=str(item.get("id") or item.get("slug")), title=item["position"],
            company=item.get("company", ""), location=item.get("location") or "Worldwide", remote=True,
            url=url, apply_url=item.get("apply_url") or url, apply_email=find_apply_email(text),
            description=text, posted_at=to_iso(item.get("epoch") or item.get("date")),
        ))
    return jobs


def fetch_weworkremotely(cfg: dict) -> list[Job]:
    content = _get("https://weworkremotely.com/remote-jobs.rss").content
    if b"<!ENTITY" in content:  # never parse XML that defines entities
        raise ValueError("feed contains entity definitions, refusing to parse")
    jobs = []
    for item in ET.fromstring(content).iter("item"):
        raw_title = (item.findtext("title") or "").strip()
        company, _, title = raw_title.partition(": ")
        if not title:
            company, title = "", raw_title
        text = html_to_text(item.findtext("description") or "")
        link = (item.findtext("link") or item.findtext("guid") or "").strip()
        jobs.append(Job(
            source="weworkremotely", external_id=(item.findtext("guid") or link).strip(), title=title.strip(),
            company=company.strip() or "Unknown", location=(item.findtext("region") or "Worldwide").strip(),
            remote=True, url=link, apply_url=link, apply_email=find_apply_email(text), description=text,
            posted_at=to_iso(item.findtext("pubDate")),
        ))
    return jobs


def fetch_adzuna(cfg: dict) -> list[Job]:
    opts = cfg["sources"]["adzuna"]
    jobs: dict[str, Job] = {}
    for title in cfg["search"]["titles"][:5]:
        params = {"app_id": opts["app_id"], "app_key": opts["app_key"], "what": title,
                  "results_per_page": 50, "max_days_old": cfg["search"]["max_age_days"],
                  "content-type": "application/json"}
        data = _get(f"https://api.adzuna.com/v1/api/jobs/{opts['country']}/search/1", params).json()
        for item in data.get("results", []):
            ext = str(item.get("id"))
            text = html_to_text(item.get("description", ""))
            location = (item.get("location") or {}).get("display_name", "")
            jobs[ext] = Job(
                source="adzuna", external_id=ext, title=html_to_text(item.get("title", "")),
                company=(item.get("company") or {}).get("display_name", "Unknown"), location=location,
                remote="remote" in (location + text[:200]).lower(), url=item.get("redirect_url", ""),
                apply_url=item.get("redirect_url", ""), apply_email=find_apply_email(text),
                description=text, posted_at=to_iso(item.get("created")),
            )
    return list(jobs.values())


# ------------------------------------------------- company career-page systems

def fetch_greenhouse(cfg: dict) -> list[Job]:
    def one(token: str) -> list[Job]:
        data = _get(f"https://boards-api.greenhouse.io/v1/boards/{quote(token)}/jobs", {"content": "true"}).json()
        out = []
        for item in data.get("jobs", []):
            location = (item.get("location") or {}).get("name", "")
            out.append(Job(
                source="greenhouse", external_id=f"{token}:{item.get('id')}", title=item.get("title", ""),
                company=item.get("company_name") or pretty(token), location=location,
                remote="remote" in location.lower(), url=item.get("absolute_url", ""),
                apply_url=item.get("absolute_url", ""), description=html_to_text(item.get("content", "")),
                posted_at=to_iso(item.get("updated_at")),
            ))
        return out
    return _per_company("greenhouse", cfg["sources"]["greenhouse"], one)


def fetch_lever(cfg: dict) -> list[Job]:
    def one(slug: str) -> list[Job]:
        out = []
        for item in _get(f"https://api.lever.co/v0/postings/{quote(slug)}", {"mode": "json"}).json():
            location = (item.get("categories") or {}).get("location", "") or ""
            out.append(Job(
                source="lever", external_id=f"{slug}:{item.get('id')}", title=item.get("text", ""),
                company=pretty(slug), location=location,
                remote=(item.get("workplaceType") == "remote") or "remote" in location.lower(),
                url=item.get("hostedUrl", ""), apply_url=item.get("applyUrl") or item.get("hostedUrl", ""),
                description=item.get("descriptionPlain", ""), posted_at=to_iso(item.get("createdAt")),
            ))
        return out
    return _per_company("lever", cfg["sources"]["lever"], one)


def fetch_ashby(cfg: dict) -> list[Job]:
    def one(name: str) -> list[Job]:
        data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{quote(name)}",
                    {"includeCompensation": "false"}).json()
        out = []
        for item in data.get("jobs", []):
            if item.get("isListed") is False:
                continue
            location = item.get("location") or ""
            text = item.get("descriptionPlain") or html_to_text(item.get("descriptionHtml", ""))
            url = item.get("jobUrl", "")
            out.append(Job(
                source="ashby", external_id=f"{name}:{item.get('id')}", title=item.get("title", ""),
                company=pretty(name), location=location,
                remote=bool(item.get("isRemote")) or "remote" in location.lower(),
                url=url, apply_url=item.get("applyUrl") or url, apply_email=find_apply_email(text),
                description=text, posted_at=to_iso(item.get("publishedAt")),
            ))
        return out
    return _per_company("ashby", cfg["sources"]["ashby"], one)


def fetch_workable(cfg: dict) -> list[Job]:
    def one(name: str) -> list[Job]:
        data = _get(f"https://apply.workable.com/api/v1/widget/accounts/{quote(name)}", {"details": "true"}).json()
        company = data.get("name") or pretty(name)
        out, seen = [], set()
        for item in data.get("jobs", []):
            code = item.get("shortcode") or item.get("id") or item.get("title")
            if code in seen:  # Workable can list one row per location
                continue
            seen.add(code)
            location = ", ".join(x for x in (item.get("city"), item.get("state"), item.get("country")) if x)
            url = item.get("url") or item.get("shortlink") or ""
            text = html_to_text(item.get("description", ""))
            out.append(Job(
                source="workable", external_id=f"{name}:{code}", title=item.get("title", ""), company=company,
                location=location, remote=bool(item.get("telecommuting")), url=url,
                apply_url=item.get("application_url") or url, apply_email=find_apply_email(text),
                description=text, posted_at=to_iso(item.get("published_on") or item.get("created_at")),
            ))
        return out
    return _per_company("workable", cfg["sources"]["workable"], one)


def fetch_smartrecruiters(cfg: dict) -> list[Job]:
    search = cfg["search"]

    def one(name: str) -> list[Job]:
        base = f"https://api.smartrecruiters.com/v1/companies/{quote(name)}/postings"
        listed, offset = [], 0
        while offset < 300:  # up to 300 postings per company
            data = _get(base, {"limit": 100, "offset": offset}).json()
            content = data.get("content", [])
            listed.extend(content)
            offset += 100
            if not content or offset >= int(data.get("totalFound", 0)):
                break
        if not listed:
            db.log("warn", f"smartrecruiters/{name}: no postings (check the company identifier)")
        company = ((listed[0].get("company") or {}).get("name") if listed else None) or pretty(name)
        out, budget = [], MAX_DETAIL_CALLS
        for item in listed:
            title = item.get("name", "")
            if not _title_plausible(title, search):
                continue  # skip jobs that could never match, saves one request each
            loc = item.get("location") or {}
            location = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), (loc.get("country") or "").upper()) if x)
            url = f"https://jobs.smartrecruiters.com/{name}/{item.get('id')}"
            text = ""
            if budget > 0:
                budget -= 1
                detail = _get(f"{base}/{item['id']}").json()
                sections = (detail.get("jobAd") or {}).get("sections") or {}
                text = " ".join(html_to_text((sections.get(key) or {}).get("text", ""))
                                for key in ("jobDescription", "qualifications", "additionalInformation"))
                url = detail.get("postingUrl") or url
            out.append(Job(
                source="smartrecruiters", external_id=f"{name}:{item.get('id')}", title=title, company=company,
                location=location, remote=bool(loc.get("remote")), url=url, apply_url=url,
                apply_email=find_apply_email(text), description=text, posted_at=to_iso(item.get("releasedDate")),
            ))
        return out
    return _per_company("smartrecruiters", cfg["sources"]["smartrecruiters"], one)


def fetch_all(cfg: dict) -> list[Job]:
    """Fetch from every enabled source; one failing source never stops the others."""
    src = cfg["sources"]
    adzuna = src.get("adzuna") or {}
    plan = [
        ("remotive", bool(src.get("remotive")), fetch_remotive),
        ("arbeitnow", bool(src.get("arbeitnow")), fetch_arbeitnow),
        ("himalayas", bool(src.get("himalayas")), fetch_himalayas),
        ("remoteok", bool(src.get("remoteok")), fetch_remoteok),
        ("weworkremotely", bool(src.get("weworkremotely")), fetch_weworkremotely),
        ("adzuna", bool(adzuna.get("enabled") and adzuna.get("app_id")), fetch_adzuna),
        ("greenhouse", bool(src.get("greenhouse")), fetch_greenhouse),
        ("lever", bool(src.get("lever")), fetch_lever),
        ("ashby", bool(src.get("ashby")), fetch_ashby),
        ("workable", bool(src.get("workable")), fetch_workable),
        ("smartrecruiters", bool(src.get("smartrecruiters")), fetch_smartrecruiters),
    ]
    found: list[Job] = []
    for name, enabled, fn in plan:
        if not enabled:
            continue
        try:
            batch = fn(cfg)
            db.log("info", f"{name}: fetched {len(batch)} postings")
            found.extend(batch)
        except Exception as exc:  # network errors, API changes
            db.log("error", f"{name}: {type(exc).__name__}: {exc}")
    return found
