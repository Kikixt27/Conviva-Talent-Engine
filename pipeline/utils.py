"""Shared pipeline helpers: hard filters before LLM calls, GitHub repo preview."""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

log = logging.getLogger("signal")

# HN hit must mention a GitHub URL (profile, repo, pages, gist) to pass hard filter.
_GITHUB_LINK = re.compile(
    r"(?:https?://)?(?:www\.)?(?:gist\.)?github\.(?:com|io)/[^\s\)\]\"'<>]+",
    re.IGNORECASE,
)

_STUDENT_TOKENS = ("tutorial", "student", "学生")

# Self-reported GitHub locations that are never a fit for US Foster City / Bay Area roles.
# Hard-skip before LLM — GitHub rarely has school pedigree; location is one of the few
# reliable pre-filters we have.
EXCLUDED_LOCATIONS = (
    "nigeria",
    "lagos",
    "abuja",
    "india",
    "bangalore",
    "bengaluru",
    "delhi",
    "hyderabad",
    "pune",
    "mumbai",
    "chennai",
    "kolkata",
    "pakistan",
    "bangladesh",
    "kenya",
    "ghana",
    "egypt",
    "indonesia",
    "philippines",
    "vietnam",
)

# Tokens that strongly suggest a US / Bay Area self-report (used for soft score caps, not hard skip).
US_LOCATION_HINTS = (
    "united states",
    "usa",
    "u.s.",
    " california",
    "ca,",
    ", ca",
    "bay area",
    "san francisco",
    "foster city",
    "palo alto",
    "mountain view",
    "sunnyvale",
    "menlo park",
    "san jose",
    "oakland",
    "berkeley",
    "seattle",
    "new york",
    "austin",
    "boston",
    "remote us",
    "us remote",
)


def location_blob_excluded(location: str | None) -> tuple[bool, str]:
    """Return (excluded, matched_token) if location clearly non-US target markets."""

    loc = (location or "").strip().lower()
    if not loc or loc in {"—", "-", "n/a", "na", "none"}:
        return False, ""
    for token in EXCLUDED_LOCATIONS:
        if token in loc:
            return True, token
    return False, ""


def location_looks_us(location: str | None) -> bool:
    loc = (location or "").strip().lower()
    if not loc:
        return False
    return any(h.strip() in loc for h in US_LOCATION_HINTS)


def github_auth_headers(token: str) -> dict[str, str]:
    h: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token.strip():
        h["Authorization"] = f"Bearer {token.strip()}"
    return h


def fetch_owned_repos_preview(
    login: str,
    *,
    headers: dict[str, str],
    timeout: int = 30,
    per_page: int = 30,
) -> list[dict[str, Any]] | None:
    """List repos for `login` (owner scope). Returns None on HTTP error (caller should not apply repo-based skip)."""

    if not login:
        return None
    url = f"https://api.github.com/users/{login}/repos"
    params = {"type": "owner", "per_page": str(per_page), "sort": "updated"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else None
    except requests.RequestException as exc:
        log.warning("GitHub repos preview failed for %s: %s", login, exc)
        return None


def fetch_repo_contributors(
    repo: str,
    *,
    headers: dict[str, str],
    timeout: int = 30,
    per_page: int = 20,
) -> list[dict[str, Any]]:
    """List top contributors for `owner/repo`. Skips bots. Empty list on error."""

    repo = (repo or "").strip().strip("/")
    if "/" not in repo:
        log.warning("Invalid github_repos entry (need owner/repo): %s", repo)
        return []
    url = f"https://api.github.com/repos/{repo}/contributors"
    params = {"per_page": str(per_page), "anon": "0"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for c in data:
            login = (c.get("login") or "").strip()
            if not login or "[bot]" in login or login.endswith("[bot]"):
                continue
            out.append(c)
        return out
    except requests.RequestException as exc:
        log.warning("GitHub contributors failed for %s: %s", repo, exc)
        return []


def enrich_github_user(
    login: str,
    *,
    headers: dict[str, str],
    timeout: int = 30,
) -> dict[str, Any] | None:
    """Fetch `/users/{login}` detail. Returns None on failure."""

    login = (login or "").strip()
    if not login:
        return None
    url = f"https://api.github.com/users/{login}"
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else None
    except requests.RequestException as exc:
        log.warning("GitHub user enrich failed for %s: %s", login, exc)
        return None


def count_owned_nonfork_repos(repos: list[dict[str, Any]] | None) -> int:
    if not repos:
        return 0
    return sum(1 for repo in repos if not repo.get("fork"))


def hard_filter_github(
    user: dict[str, Any],
    owned_repos_preview: list[dict[str, Any]] | None,
) -> tuple[bool, str]:
    """Return (skip, reason). Skip=True means do not call the LLM.

    Rules:
    - Self-reported location matches EXCLUDED_LOCATIONS (Nigeria/India/…) → skip
    - Bio empty AND followers < 5 → skip
    - Bio/company/name/login contains tutorial / student / 学生 → skip
    - owned non-fork repos < 1 when preview is available (not None) → skip
    """

    login = (user.get("login") or "").strip()
    bio = (user.get("bio") or "").strip()
    company = (user.get("company") or "").strip()
    name = (user.get("name") or "").strip()
    followers = int(user.get("followers") or 0)

    excluded, token = location_blob_excluded(user.get("location"))
    if excluded:
        return True, f"hard_filter:excluded_location:{token}"

    if not bio and followers < 5:
        return True, "hard_filter:bio_empty_and_followers_lt_5"

    blob = f"{bio} {company} {name} {login}".lower()
    for tok in _STUDENT_TOKENS:
        if tok in blob:
            return True, f"hard_filter:keyword_{tok.replace('/', '_')}"

    if owned_repos_preview is not None:
        owned = count_owned_nonfork_repos(owned_repos_preview)
        if owned < 1:
            return True, "hard_filter:owned_nonfork_repos_lt_1"

    return False, ""


def hackernews_text_blob(hit: dict[str, Any]) -> str:
    parts = [
        str(hit.get("comment_text") or ""),
        str(hit.get("story_text") or ""),
        str(hit.get("title") or ""),
        str(hit.get("story_title") or ""),
        str(hit.get("url") or ""),
        str(hit.get("story_url") or ""),
    ]
    return " ".join(parts)


def hard_filter_hackernews(hit: dict[str, Any]) -> tuple[bool, str]:
    """HN channel: skip if no GitHub link in comment/story/titles/urls."""

    if _GITHUB_LINK.search(hackernews_text_blob(hit)):
        return False, ""
    return True, "hard_filter:hn_no_github_link"
