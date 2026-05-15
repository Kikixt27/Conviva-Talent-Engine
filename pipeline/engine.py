"""Conviva Signal — nightly sourcing engine (canonical implementation).

GitHub / HackerNews search → hard filters (`pipeline.utils`) → LLM scoring →
`data/candidates.json`, daily JSONL under `data/candidates/`, HTML under `reports/`.

Entry points:
    python -m pipeline.scorer
    python scripts/source.py   # thin forwarder to this module
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

from pipeline.utils import (
    fetch_owned_repos_preview,
    github_auth_headers,
    hard_filter_github,
    hard_filter_hackernews,
)

ROOT = Path(__file__).resolve().parent.parent
ROLES_FILE = ROOT / "roles.json"
ROLES_DIR = ROOT / "data" / "roles"
DATA_FILE = ROOT / "data" / "candidates.json"
CANDIDATES_RUN_DIR = ROOT / "data" / "candidates"
REPORTS_DIR = ROOT / "reports"
FEEDBACK_FILE = Path(os.environ.get("FEEDBACK_FILE", str(ROOT / "feedback" / "entries.jsonl")))

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

SKIP_REJECTED_CANDIDATES = os.environ.get("SKIP_REJECTED_CANDIDATES", "0").strip() in {"1", "true", "yes"}

FEEDBACK_REJECT_LABELS = frozenset({"reject", "bad_fit", "no"})

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
    """Prefer `data/roles/*.json` (one role per file, sorted); else root `roles.json`."""

    if ROLES_DIR.is_dir():
        files = sorted(ROLES_DIR.glob("*.json"))
        if files:
            roles: list[dict[str, Any]] = []
            for path in files:
                with path.open("r", encoding="utf-8") as fh:
                    roles.append(json.load(fh))
            log.info("Loaded %d role(s) from %s", len(roles), ROLES_DIR)
            return roles
    if not ROLES_FILE.exists():
        log.error("No roles found: add JSON files under %s or create %s", ROLES_DIR, ROLES_FILE)
        sys.exit(1)
    with ROLES_FILE.open("r", encoding="utf-8") as fh:
        log.info("Loaded roles from legacy %s", ROLES_FILE)
        return json.load(fh)


def load_seen_candidates() -> dict[str, dict[str, Any]]:
    if not DATA_FILE.exists():
        return {}
    with DATA_FILE.open("r", encoding="utf-8") as fh:
        records = json.load(fh)
    return {f"{r['source']}:{r['source_id']}": r for r in records}


def append_daily_candidates_jsonl(day: str, new_candidates: list[Candidate]) -> None:
    """Append this run's new rows to data/candidates/YYYY-MM-DD.jsonl (optional audit trail)."""

    if not new_candidates:
        return
    CANDIDATES_RUN_DIR.mkdir(parents=True, exist_ok=True)
    path = CANDIDATES_RUN_DIR / f"{day}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for cand in new_candidates:
            fh.write(json.dumps(asdict(cand), ensure_ascii=False) + "\n")
    log.info("Appended %d row(s) to %s", len(new_candidates), path)


def save_candidates(seen: dict[str, dict[str, Any]]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("w", encoding="utf-8") as fh:
        json.dump(list(seen.values()), fh, ensure_ascii=False, indent=2)


def search_github(query: str, language: str | None, location: str | None) -> Iterable[dict[str, Any]]:
    """Query the GitHub user search API."""

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


def read_feedback_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse one JSONL file; skip bad lines."""

    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Could not read feedback file %s: %s", path, exc)
        return []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("Skipping invalid JSONL at %s line %s", path, lineno)
    return rows


def load_all_feedback_entries() -> list[dict[str, Any]]:
    """Merge every `feedback/*.jsonl` except `*.example` (dated files + entries.jsonl)."""

    fb_dir = ROOT / "feedback"
    if not fb_dir.is_dir():
        return read_feedback_jsonl(FEEDBACK_FILE)
    merged: list[dict[str, Any]] = []
    for path in sorted(fb_dir.glob("*.jsonl")):
        if path.name.endswith(".example"):
            continue
        merged.extend(read_feedback_jsonl(path))
    merged.sort(key=lambda e: str(e.get("ts", "")), reverse=True)
    return merged


def feedback_dedup_key(entry: dict[str, Any]) -> str:
    return f"{entry.get('source', '').strip().lower()}:{entry.get('source_id', '').strip()}"


def rejected_dedup_keys(entries: Iterable[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for e in entries:
        lab = str(e.get("label", "")).strip().lower()
        if lab in FEEDBACK_REJECT_LABELS:
            out.add(feedback_dedup_key(e))
    return out


def format_feedback_for_prompt(role_title: str, entries: list[dict[str, Any]]) -> str:
    """Build a short calibration block from recent TA labels (cost-effective 'learning')."""

    if not entries:
        return ""

    per_role = [e for e in entries if (e.get("role") or "").strip() == role_title]
    per_role.sort(key=lambda e: str(e.get("ts", "")), reverse=True)
    per_role = per_role[:18]

    global_rej = [e for e in entries if str(e.get("label", "")).strip().lower() in FEEDBACK_REJECT_LABELS]
    global_rej.sort(key=lambda e: str(e.get("ts", "")), reverse=True)
    global_rej = global_rej[:8]

    chunks: list[str] = []
    if per_role:
        lines = []
        for e in per_role:
            reason = (e.get("reason") or "").strip() or "(no reason)"
            lines.append(
                f"- [{e.get('label')}] {e.get('source')}:{e.get('source_id')} — {reason}"
            )
        chunks.append(
            "TA calibration for THIS role (use to calibrate strictness; do not copy scores blindly):\n"
            + "\n".join(lines)
        )
    if global_rej:
        lines = []
        for e in global_rej:
            r = (e.get("reason") or "").strip()[:220]
            lines.append(
                f"- {e.get('source')}:{e.get('source_id')} role={e.get('role')} — {r}"
            )
        chunks.append(
            "Recent rejections across roles (penalize similar weak evidence / false positives):\n" + "\n".join(lines)
        )
    if not chunks:
        return ""
    return (
        "\n\n"
        + "\n\n".join(chunks)
        + "\n\nIf this candidate is clearly the same pattern as a recent rejection "
        "(same channel id, or same failure mode described in reasons), score lower unless "
        "the profile shows strong contradictory evidence."
    )


def score_candidate(
    role: dict[str, Any],
    profile_blob: str,
    *,
    feedback_tail: str = "",
) -> dict[str, Any] | None:
    """Ask the configured LLM to rate this candidate from 0-100."""

    prompt = (
        "You are a senior technical recruiter at Conviva. Score this candidate "
        f"for the role '{role['title']}' from 0 to 100 based on the profile.\n\n"
        f"Role requirements:\n{role.get('requirements', '')}\n"
        f"{feedback_tail}\n"
        f"Candidate profile:\n{profile_blob}\n\n"
        "Be conservative: weak evidence (e.g. a single HN comment without corroboration) "
        "should not score above 75 unless requirements are clearly met.\n\n"
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


def collect_for_role(
    role: dict[str, Any],
    seen: dict[str, dict[str, Any]],
    *,
    feedback_entries: list[dict[str, Any]],
    reject_keys: set[str],
) -> list[Candidate]:
    """Run every search source for one role and return new scored candidates."""

    fresh: list[Candidate] = []
    feedback_tail = format_feedback_for_prompt(role["title"], feedback_entries)

    for query in role.get("github_queries", []):
        for user in search_github(query, role.get("language"), role.get("location")):
            key = f"github:{user.get('id')}"
            if key in seen:
                continue
            if SKIP_REJECTED_CANDIDATES and key in reject_keys:
                log.info("Skip (TA feedback): %s", key)
                continue
            gh_headers = github_auth_headers(GITHUB_TOKEN)
            login = (user.get("login") or "").strip()
            repos_preview: list[dict[str, Any]] | None = None
            if login:
                time.sleep(0.35)
                repos_preview = fetch_owned_repos_preview(
                    login, headers=gh_headers, timeout=HTTP_TIMEOUT
                )
            skip_hf, hf_reason = hard_filter_github(user, repos_preview)
            if skip_hf:
                log.info("Skip (hard_filter) github:%s — %s", login or key, hf_reason)
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
            scored = score_candidate(role, blob, feedback_tail=feedback_tail)
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
            if SKIP_REJECTED_CANDIDATES and key in reject_keys:
                log.info("Skip (TA feedback): %s", key)
                continue
            skip_hf, hf_reason = hard_filter_hackernews(hit)
            if skip_hf:
                log.info("Skip (hard_filter) hackernews:%s — %s", author, hf_reason)
                continue
            blob = (
                f"HN handle: {author}\n"
                f"Story title: {hit.get('story_title') or hit.get('title') or '—'}\n"
                f"Comment / text: {(hit.get('comment_text') or hit.get('story_text') or '')[:600]}\n"
                f"URL: https://news.ycombinator.com/user?id={author}"
            )
            scored = score_candidate(role, blob, feedback_tail=feedback_tail)
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
    feedback_entries = load_all_feedback_entries()
    reject_keys = rejected_dedup_keys(feedback_entries)
    log.info("Loaded %d role(s); %d candidates already in database.", len(roles), len(seen))
    log.info(
        "Feedback: %d entries from %s; reject keys=%d; skip_rejected=%s",
        len(feedback_entries),
        FEEDBACK_FILE,
        len(reject_keys),
        SKIP_REJECTED_CANDIDATES,
    )

    new_candidates: list[Candidate] = []
    for role in roles:
        log.info("=== Sourcing for role: %s ===", role["title"])
        fresh = collect_for_role(
            role,
            seen,
            feedback_entries=feedback_entries,
            reject_keys=reject_keys,
        )
        log.info("Found %d new candidate(s) for %s", len(fresh), role["title"])
        for cand in fresh:
            seen[cand.dedup_key] = asdict(cand)
        new_candidates.extend(fresh)

    save_candidates(seen)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    append_daily_candidates_jsonl(day, new_candidates)
    report = render_report(new_candidates)
    post_slack_digest(new_candidates, report)
    log.info("Done. Total stored candidates: %d", len(seen))
    return 0
