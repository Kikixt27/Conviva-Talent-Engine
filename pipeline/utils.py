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
    - Bio empty AND followers < 5 → skip
    - Bio/company/name/login contains tutorial / student / 学生 → skip
    - owned non-fork repos < 1 when preview is available (not None) → skip
    """

    login = (user.get("login") or "").strip()
    bio = (user.get("bio") or "").strip()
    company = (user.get("company") or "").strip()
    name = (user.get("name") or "").strip()
    followers = int(user.get("followers") or 0)

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
