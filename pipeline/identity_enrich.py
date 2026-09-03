"""Identity enrichment tools for nickname-only GitHub profiles.

Goal: before pausing for human LinkedIn paste, automatically pull clues from:
  - GitHub user API (blog, twitter, email, company, bio)
  - Public commit/event emails (when exposed)
  - Direct Kaggle profile probe: kaggle.com/{login}
  - Stack Overflow user search API (by login/display name)

This is NOT LinkedIn scraping. It reduces blind Google X-Ray for handles.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import quote

import requests

log = logging.getLogger("signal.enrich")

HTTP_TIMEOUT = int(os.environ.get("ENRICH_HTTP_TIMEOUT", "20"))

_TOP_SCHOOL_PATTERNS = (
    r"\bmit\b",
    r"massachusetts institute of technology",
    r"\bstanford\b",
    r"\bcmu\b",
    r"carnegie mellon",
    r"\bberkeley\b",
    r"uc berkeley",
    r"\bucla\b",
    r"\bcornell\b",
    r"\buiuc\b",
    r"university of illinois",
    r"university of michigan",
    r"\bumich\b",
    r"\bduke\b",
    r"caltech",
    r"georgia tech",
    r"university of washington",
    r"\bwisc\b",
    r"princeton",
    r"harvard",
    r"yale",
    r"columbia university",
)

_EDU_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.edu\b", re.I)
_ANY_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.I)


def _gh_headers() -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Conviva-Signal-Enrich/1.0",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _get(url: str, *, headers: dict[str, str] | None = None, timeout: int = HTTP_TIMEOUT) -> Any:
    try:
        r = requests.get(url, headers=headers or {"User-Agent": "Conviva-Signal-Enrich/1.0"}, timeout=timeout)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        ctype = (r.headers.get("content-type") or "").lower()
        if "application/json" in ctype:
            return r.json()
        return r.text
    except requests.RequestException as exc:
        log.info("enrich GET failed %s: %s", url, exc)
        return None


def extract_school_hits(text: str) -> list[str]:
    blob = (text or "").lower()
    hits: list[str] = []
    for pat in _TOP_SCHOOL_PATTERNS:
        if re.search(pat, blob, flags=re.I):
            hits.append(pat.replace(r"\b", "").replace("\\", ""))
    # dedupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def enrich_github_identity(login: str) -> dict[str, Any]:
    """Pull GH profile fields + optional public event emails."""

    login = (login or "").strip()
    out: dict[str, Any] = {"source": "github", "login": login, "ok": False}
    if not login:
        return out

    user = _get(f"https://api.github.com/users/{login}", headers=_gh_headers())
    if not isinstance(user, dict):
        out["error"] = "github_user_not_found"
        return out

    out["ok"] = True
    out["name"] = user.get("name") or ""
    out["company"] = user.get("company") or ""
    out["blog"] = (user.get("blog") or "").strip()
    out["bio"] = user.get("bio") or ""
    out["location"] = user.get("location") or ""
    out["twitter"] = user.get("twitter_username") or ""
    out["email"] = user.get("email") or ""
    out["hireable"] = user.get("hireable")
    out["html_url"] = user.get("html_url") or f"https://github.com/{login}"

    emails: list[str] = []
    if out["email"]:
        emails.append(str(out["email"]))

    # Public events sometimes expose commit emails
    events = _get(
        f"https://api.github.com/users/{login}/events/public?per_page=30",
        headers=_gh_headers(),
    )
    if isinstance(events, list):
        for ev in events[:30]:
            payload = (ev or {}).get("payload") or {}
            for commit in payload.get("commits") or []:
                em = ((commit.get("author") or {}).get("email") or "").strip()
                if em and "noreply.github.com" not in em.lower():
                    emails.append(em)

    # unique
    seen_e: set[str] = set()
    uniq_emails: list[str] = []
    for e in emails:
        el = e.lower()
        if el not in seen_e:
            seen_e.add(el)
            uniq_emails.append(e)
    out["emails"] = uniq_emails[:8]
    out["edu_emails"] = [e for e in uniq_emails if e.lower().endswith(".edu")]

    # Fetch blog/about homepage text (short) for school tokens
    blog = out["blog"]
    if blog:
        if blog.startswith("http"):
            blog_url = blog
        else:
            blog_url = "https://" + blog.lstrip("/")
        out["blog_url"] = blog_url
        html = _get(blog_url)
        if isinstance(html, str) and html:
            # strip tags lightly
            text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
            text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text)[:4000]
            out["blog_text_preview"] = text[:800]
            out["blog_school_hits"] = extract_school_hits(text)
        else:
            out["blog_school_hits"] = []
    else:
        out["blog_school_hits"] = []

    bio_hits = extract_school_hits(f"{out['bio']} {out['company']}")
    out["bio_school_hits"] = bio_hits
    return out


def probe_kaggle(login: str) -> dict[str, Any]:
    """HEAD/GET kaggle.com/{login} — exact handle match only."""

    login = (login or "").strip()
    out: dict[str, Any] = {"source": "kaggle", "login": login, "exists": False}
    if not login:
        return out
    url = f"https://www.kaggle.com/{quote(login)}"
    out["url"] = url
    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Conviva-Signal-Enrich/1.0"},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
        # Kaggle may 200 even for missing with soft page — check title/body heuristics
        text = (r.text or "")[:3000].lower()
        if r.status_code == 200 and "page not found" not in text and "404" not in (r.url or ""):
            # weak positive: profile-ish markers
            if login.lower() in text or "competitions" in text or "datasets" in text:
                out["exists"] = True
                out["school_hits"] = extract_school_hits(text)
        elif r.status_code == 404:
            out["exists"] = False
        else:
            out["status_code"] = r.status_code
    except requests.RequestException as exc:
        out["error"] = str(exc)
    return out


def search_stackoverflow(login: str) -> dict[str, Any]:
    """Stack Exchange API user search by display name / handle."""

    login = (login or "").strip()
    out: dict[str, Any] = {"source": "stackoverflow", "login": login, "matches": []}
    if not login:
        return out
    url = (
        "https://api.stackexchange.com/2.3/users"
        f"?order=desc&sort=reputation&inname={quote(login)}"
        "&site=stackoverflow&pagesize=5&filter=default"
    )
    data = _get(url)
    if not isinstance(data, dict):
        out["error"] = "so_api_failed"
        return out
    for u in data.get("items") or []:
        out["matches"].append(
            {
                "user_id": u.get("user_id"),
                "display_name": u.get("display_name"),
                "link": u.get("link"),
                "reputation": u.get("reputation"),
                "location": u.get("location"),
            }
        )
    return out


def summarize_enrichment(parts: dict[str, Any]) -> dict[str, Any]:
    """Merge tool results into agent-facing clues + whether auto re-score is warranted."""

    clues: list[str] = []
    school_hits: list[str] = []
    links: list[dict[str, str]] = []
    emails_edu: list[str] = []

    gh = parts.get("github") or {}
    if gh.get("ok"):
        if gh.get("name"):
            clues.append(f"GitHub name field: {gh['name']}")
        if gh.get("company"):
            clues.append(f"GitHub company: {gh['company']}")
        if gh.get("twitter"):
            clues.append(f"Twitter/X: @{gh['twitter']}")
            links.append(
                {
                    "label": "Twitter/X",
                    "url": f"https://twitter.com/{gh['twitter']}",
                }
            )
        if gh.get("blog_url") or gh.get("blog"):
            bu = gh.get("blog_url") or gh.get("blog")
            clues.append(f"Blog/site: {bu}")
            links.append({"label": "Personal site / blog", "url": bu})
        for e in gh.get("edu_emails") or []:
            emails_edu.append(e)
            clues.append(f"Edu email: {e}")
        for e in gh.get("emails") or []:
            if e not in (gh.get("edu_emails") or []):
                clues.append(f"Public email: {e}")
        school_hits.extend(gh.get("bio_school_hits") or [])
        school_hits.extend(gh.get("blog_school_hits") or [])

    kg = parts.get("kaggle") or {}
    if kg.get("exists"):
        clues.append(f"Kaggle profile exists: {kg.get('url')}")
        links.append({"label": "Kaggle profile", "url": kg.get("url") or ""})
        school_hits.extend(kg.get("school_hits") or [])
    elif kg.get("url"):
        clues.append("Kaggle: no profile for this exact login")

    so = parts.get("stackoverflow") or {}
    for m in (so.get("matches") or [])[:3]:
        clues.append(
            f"StackOverflow: {m.get('display_name')} (rep {m.get('reputation')}) {m.get('link')}"
        )
        if m.get("link"):
            links.append({"label": f"SO · {m.get('display_name')}", "url": m["link"]})
        if m.get("location"):
            clues.append(f"SO location: {m['location']}")

    # dedupe school hits
    seen: set[str] = set()
    schools: list[str] = []
    for h in school_hits:
        if h not in seen:
            seen.add(h)
            schools.append(h)

    strong = bool(emails_edu) or bool(schools)
    confidence = "high" if emails_edu or len(schools) >= 1 else ("medium" if clues else "low")

    education_blob_parts = ["--- AUTO IDENTITY ENRICHMENT ---"]
    education_blob_parts.extend(f"- {c}" for c in clues[:20])
    if schools:
        education_blob_parts.append("School hits: " + ", ".join(schools))
    if emails_edu:
        education_blob_parts.append("Edu emails: " + ", ".join(emails_edu))
    if not clues:
        education_blob_parts.append(
            "Enrichment found no blog/twitter/edu email/Kaggle/SO match. "
            "Nickname-only GitHub likely; human LinkedIn still required."
        )

    return {
        "clues": clues,
        "school_hits": schools,
        "edu_emails": emails_edu,
        "links": [x for x in links if x.get("url")],
        "strong_pedigree_signal": strong,
        "confidence": confidence,
        "education_blob": "\n".join(education_blob_parts),
        "raw": parts,
    }


def enrich_identity(login: str) -> dict[str, Any]:
    """Run all enrichment tools for a GitHub login."""

    login = (login or "").strip()
    parts = {
        "github": enrich_github_identity(login) if login else {},
        "kaggle": probe_kaggle(login) if login else {},
        "stackoverflow": search_stackoverflow(login) if login else {},
    }
    summary = summarize_enrichment(parts)
    summary["login"] = login
    log.info(
        "enrich %s → confidence=%s schools=%s edu_emails=%s clues=%d",
        login,
        summary["confidence"],
        summary["school_hits"],
        summary["edu_emails"],
        len(summary["clues"]),
    )
    return summary
