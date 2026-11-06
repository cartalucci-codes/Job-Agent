"""Score how well a job fits the search description (0-100)."""
from __future__ import annotations

import re


def _has(term: str, text: str) -> bool:
    pattern = r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text) is not None


GENERIC_REMOTE = {"remote", "worldwide", "anywhere", "global", "global remote", "anywhere in the world"}


def remote_usable(location: str, regions: list[str]) -> bool:
    """A remote job is usable when it has no region limit, or the limit includes your region."""
    loc = (location or "").lower().strip()
    if not loc or loc in GENERIC_REMOTE:
        return True
    return any(r.lower() in loc for r in regions)


def matched_skills(job, search: dict) -> list[str]:
    text = f"{job['title']} {job['description']}".lower()
    return [s for s in search["skills"] if _has(s, text)]


def score_job(job, search: dict) -> tuple[int, list[str]]:
    """`job` may be a Job dataclass-like mapping (dict / sqlite Row)."""
    title = (job["title"] or "").lower()
    description = (job["description"] or "").lower()
    text = f"{title} {description}"
    reasons: list[str] = []

    for bad in search["exclude_title"]:
        if _has(bad, title):
            return 0, [f"excluded: title contains '{bad}'"]
    for bad in search["exclude_description"]:
        if _has(bad, description):
            return 0, [f"excluded: description contains '{bad}'"]

    # Title (40)
    title_pts = 0
    for wanted in search["titles"]:
        w = wanted.lower()
        if _has(w, title):
            title_pts, hit = 40, wanted
            reasons.append(f"title matches '{hit}'")
            break
        words = [x for x in re.split(r"\W+", w) if x]
        if words:
            overlap = sum(1 for x in words if _has(x, title)) / len(words)
            if overlap >= 0.5:
                pts = round(30 * overlap)
                if pts > title_pts:
                    title_pts, hit = pts, wanted
    else:
        if title_pts:
            reasons.append(f"title partly matches '{hit}'")
    if not title_pts:
        reasons.append("title does not match")

    # Required skills (30): at least one must be present
    skills = [s for s in search["skills"] if _has(s, text)]
    skill_pts = 0
    if search["skills"]:
        if skills:
            skill_pts = round(30 * min(len(skills), 3) / 3)
            reasons.append("skills: " + ", ".join(skills[:6]))
        else:
            reasons.append("none of your skills mentioned")

    # Nice to have (15)
    nice = [s for s in search["nice_to_have"] if _has(s, text)]
    nice_pts = round(15 * min(len(nice), 3) / 3)
    if nice:
        reasons.append("bonus: " + ", ".join(nice[:5]))

    # Location (15)
    location = (job["location"] or "").lower()
    places = [l.lower() for l in search["locations"] if l.lower() != "remote"]
    loc_pts = 0
    if search["remote_ok"] and job["remote"] and remote_usable(location, search.get("remote_regions", []) + places):
        loc_pts = 15
        reasons.append("remote" + (f" ({job['location']})" if location and location not in GENERIC_REMOTE else ""))
    elif any(p in location for p in places):
        loc_pts = 15
        reasons.append(f"location: {job['location']}")
    elif job["remote"]:
        reasons.append(f"remote, but limited to {job['location']}")
    elif not location:
        loc_pts = 7
        reasons.append("location not stated")
    else:
        reasons.append(f"location mismatch ({job['location']})")

    total = title_pts + skill_pts + nice_pts + loc_pts
    if search["skills"] and not skills:
        total = min(total, search["min_score"] - 1)  # a job with none of your skills is never queued
    return max(0, min(100, total)), reasons
