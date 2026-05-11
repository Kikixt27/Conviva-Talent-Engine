"""Conviva Signal — Nightly Sourcing Engine.

Searches GitHub / HackerNews / Product Hunt for candidates matching role
queries defined in roles.json, scores them with Claude or DeepSeek, persists
unique hits to data/candidates.json, writes a daily HTML report into reports/,
and posts a Slack digest if SLACK_WEBHOOK_URL is configured.

Designed to be run unattended by GitHub Actions, but also works locally:
    python scripts/source.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

ROOT = Path(__file__).resolve().parent.parent
ROLES_FILE = ROOT / "roles.json"
DATA_FILE = ROOT / "data" / "candidates.json"
REPORTS_DIR = ROOT / "reports"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "").strip()
DEEPSEEK_API_KEY = os.environ.get("DS_API_KEY", "").strip()
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

AI_PROVIDER = os.environ.get("AI_PROVIDER", "auto").strip().lower()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5").strip()
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat").strip()
SCORE_THRESHOLD = int(os.environ.get("SCORE_THRESHOLD", "70"))
MAX_CANDIDATES_PER_QUERY = int(os.environ.get("MAX_CANDIDATES_PER_QUERY", "15"))
HTTP_TIMEOUT = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("signal")


@dataclass
class Candidate:
    """A scored sourcing hit, persisted in data/candidates.json."""

    source: str
    source_id: str
    name: str
    profile_url: str
    role: str
    score: int
    reasoning: str
    signals: dict[str, Any] = field(default_factory=dict)
    first_seen: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def dedup_key(self) -> str:
        return f"{self.source}:{self.source_id}"


def load_roles() -> list[dict[str, Any]]:
    if not ROLES_FILE.exists():
        log.error("roles.json not found at %s — create it before running.", ROLES_FILE)
        sys.exit(1)
    with ROLES_FILE.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_seen_candidates() -> dict[str, dict[str, Any]]:
    if not DATA_FILE.exists():
        return {}
    with DATA_FILE.open("r", encoding="utf-8") as fh:
        records = json.load(fh)
    return {f"{r['source']}:{r['source_id']}": r for r in records}


def save_candidates(seen: dict[str, dict[str, Any]]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("w", encoding="utf-8") as fh:
        json.dump(list(seen.values()), fh, ensure_ascii=False, indent=2)


def search_github(query: str, language: str | None, location: str | None) -> Iterable[dict[str, Any]]:
    """Query the GitHub user search API.

    Uses the official users endpoint; honours the public 30 req/min rate limit
    when no token is provided. With GITHUB_TOKEN the limit jumps to 5000/hr.
    """

    parts = [query]
    if language:
        parts.append(f"language:{language}")
    if location:
        parts.append(f"location:{location}")

    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    params = {"q": " ".join(parts), "per_page": MAX_CANDIDATES_PER_QUERY}
    url = "https://api.github.com/search/users"

    log.info("GitHub search: %s", params["q"])
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("GitHub search failed (%s) — skipping", exc)
        return []

    items = resp.json().get("items", [])
    enriched = []
    for item in items:
        time.sleep(0.4)
        try:
            detail = requests.get(item["url"], headers=headers, timeout=HTTP_TIMEOUT)
            detail.raise_for_status()
            enriched.append(detail.json())
        except requests.RequestException:
            enriched.append(item)
    return enriched


def search_hackernews(query: str) -> Iterable[dict[str, Any]]:
    """Pull recent HN comments / stories matching query via Algolia."""

    url = "https://hn.algolia.com/api/v1/search_by_date"
    params = {"query": query, "tags": "(comment,story)", "hitsPerPage": MAX_CANDIDATES_PER_QUERY}
    log.info("HackerNews search: %s", query)
    try:
        resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("HackerNews search failed (%s) — skipping", exc)
        return []
    return resp.json().get("hits", [])


def score_with_claude(prompt: str) -> dict[str, Any] | None:
    if not CLAUDE_API_KEY:
        return None
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": CLAUDE_MODEL,
        "max_tokens": 600,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers, json=body, timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Claude scoring failed: %s", exc)
        return None
    text = resp.json()["content"][0]["text"]
    return _extract_json(text)


def score_with_deepseek(prompt: str) -> dict[str, Any] | None:
    if not DEEPSEEK_API_KEY:
        return None
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }
    try:
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers=headers, json=body, timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("DeepSeek scoring failed: %s", exc)
        return None
    text = resp.json()["choices"][0]["message"]["content"]
    return _extract_json(text)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of an LLM response."""

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def score_candidate(role: dict[str, Any], profile_blob: str) -> dict[str, Any] | None:
    """Ask the configured LLM to rate this candidate from 0-100."""

    prompt = (
        "You are a senior technical recruiter at Conviva. Score this candidate "
        f"for the role '{role['title']}' from 0 to 100 based on the profile.\n\n"
        f"Role requirements:\n{role.get('requirements', '')}\n\n"
        f"Candidate profile:\n{profile_blob}\n\n"
        "Respond ONLY with a JSON object with keys: score (int 0-100), "
        "reasoning (1-2 sentences), top_signal (one short phrase)."
    )

    providers = []
    if AI_PROVIDER in {"claude", "auto"}:
        providers.append(("claude", score_with_claude))
    if AI_PROVIDER in {"deepseek", "auto"}:
        providers.append(("deepseek", score_with_deepseek))

    for name, fn in providers:
        result = fn(prompt)
        if result and "score" in result:
            log.info("Scored via %s: %s", name, result["score"])
            return result
    log.warning("No AI provider returned a valid score — skipping candidate")
    return None


def collect_for_role(role: dict[str, Any], seen: dict[str, dict[str, Any]]) -> list[Candidate]:
    """Run every search source for one role and return new scored candidates."""

    fresh: list[Candidate] = []

    for query in role.get("github_queries", []):
        for user in search_github(query, role.get("language"), role.get("location")):
            key = f"github:{user.get('id')}"
            if key in seen:
                continue
            blob = (
                f"Name: {user.get('name') or user.get('login')}\n"
                f"Bio: {user.get('bio') or '—'}\n"
                f"Location: {user.get('location') or '—'}\n"
                f"Company: {user.get('company') or '—'}\n"
                f"Public repos: {user.get('public_repos', 0)}\n"
                f"Followers: {user.get('followers', 0)}\n"
                f"Profile: {user.get('html_url')}"
            )
            scored = score_candidate(role, blob)
            if not scored or scored.get("score", 0) < SCORE_THRESHOLD:
                continue
            fresh.append(Candidate(
                source="github",
                source_id=str(user.get("id")),
                name=user.get("name") or user.get("login", "unknown"),
                profile_url=user.get("html_url", ""),
                role=role["title"],
                score=int(scored["score"]),
                reasoning=scored.get("reasoning", ""),
                signals={
                    "top_signal": scored.get("top_signal", ""),
                    "followers": user.get("followers", 0),
                    "public_repos": user.get("public_repos", 0),
                    "company": user.get("company"),
                    "location": user.get("location"),
                },
            ))

    for query in role.get("hn_queries", []):
        for hit in search_hackernews(query):
            author = hit.get("author")
            if not author:
                continue
            key = f"hackernews:{author}"
            if key in seen:
                continue
            blob = (
                f"HN handle: {author}\n"
                f"Story title: {hit.get('story_title') or hit.get('title') or '—'}\n"
                f"Comment / text: {(hit.get('comment_text') or hit.get('story_text') or '')[:600]}\n"
                f"URL: https://news.ycombinator.com/user?id={author}"
            )
            scored = score_candidate(role, blob)
            if not scored or scored.get("score", 0) < SCORE_THRESHOLD:
                continue
            fresh.append(Candidate(
                source="hackernews",
                source_id=author,
                name=author,
                profile_url=f"https://news.ycombinator.com/user?id={author}",
                role=role["title"],
                score=int(scored["score"]),
                reasoning=scored.get("reasoning", ""),
                signals={"top_signal": scored.get("top_signal", "")},
            ))
    return fresh


def render_report(new_candidates: list[Candidate]) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_path = REPORTS_DIR / f"{today}.html"

    rows = []
    for cand in sorted(new_candidates, key=lambda c: c.score, reverse=True):
        rows.append(
            "<tr>"
            f"<td>{cand.score}</td>"
            f"<td>{cand.role}</td>"
            f"<td><a href='{cand.profile_url}' target='_blank'>{cand.name}</a></td>"
            f"<td>{cand.source}</td>"
            f"<td>{cand.signals.get('top_signal', '')}</td>"
            f"<td>{cand.reasoning}</td>"
            "</tr>"
        )

    html = f"""<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\">
<title>Conviva Signal — {today}</title>
<style>
  body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:32px;color:#111}}
  h1{{margin-bottom:4px}}
  .meta{{color:#666;margin-bottom:24px}}
  table{{border-collapse:collapse;width:100%}}
  th,td{{border-bottom:1px solid #eee;padding:10px 12px;text-align:left;vertical-align:top}}
  th{{background:#fafafa;font-weight:600}}
  tr:hover{{background:#f7f9fc}}
</style></head>
<body>
<h1>Conviva Signal — {today}</h1>
<div class=\"meta\">{len(new_candidates)} new candidate(s) at score &ge; {SCORE_THRESHOLD}</div>
<table>
  <thead><tr><th>Score</th><th>Role</th><th>Candidate</th><th>Source</th><th>Top signal</th><th>Reasoning</th></tr></thead>
  <tbody>{''.join(rows) or '<tr><td colspan=6>No new candidates today.</td></tr>'}</tbody>
</table>
</body></html>"""
    report_path.write_text(html, encoding="utf-8")
    log.info("Report written: %s", report_path)
    return report_path


def post_slack_digest(new_candidates: list[Candidate], report_path: Path) -> None:
    if not SLACK_WEBHOOK_URL:
        log.info("SLACK_WEBHOOK_URL not set — skipping Slack notification")
        return
    if not new_candidates:
        text = "Conviva Signal: no new candidates today."
    else:
        top = max(new_candidates, key=lambda c: c.score)
        text = (
            f":mag: Conviva Signal — {len(new_candidates)} new candidate(s).\n"
            f"Top: *{top.name}* ({top.role}) — score {top.score}.\n"
            f"Report artifact: `{report_path.name}`"
        )
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        log.warning("Slack post failed: %s", exc)


def main() -> int:
    if not CLAUDE_API_KEY and not DEEPSEEK_API_KEY:
        log.error("Set CLAUDE_API_KEY or DS_API_KEY before running.")
        return 2

    roles = load_roles()
    seen = load_seen_candidates()
    log.info("Loaded %d role(s); %d candidates already in database.", len(roles), len(seen))

    new_candidates: list[Candidate] = []
    for role in roles:
        log.info("=== Sourcing for role: %s ===", role["title"])
        fresh = collect_for_role(role, seen)
        log.info("Found %d new candidate(s) for %s", len(fresh), role["title"])
        for cand in fresh:
            seen[cand.dedup_key] = asdict(cand)
        new_candidates.extend(fresh)

    save_candidates(seen)
    report = render_report(new_candidates)
    post_slack_digest(new_candidates, report)
    log.info("Done. Total stored candidates: %d", len(seen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
