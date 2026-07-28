"""Local accuracy test — one or more roles, optional dry-run (no writes to data/candidates.json).

Examples:
  # Search + hard-filter only (no API keys):
  python scripts/test_sourcing.py --role "Product Builder" --search-only

  # Full pipeline slice with scoring (needs CLAUDE_API_KEY or DS_API_KEY):
  $env:CLAUDE_API_KEY = "sk-ant-..."
  python scripts/test_sourcing.py --role "Tech Lead" --max-per-query 3 --threshold 60 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import engine
from pipeline.utils import fetch_owned_repos_preview, github_auth_headers, hard_filter_github, hard_filter_hackernews


def _pick_roles(needle: str | None) -> list[dict]:
    roles = engine.load_roles()
    if not needle:
        return roles
    key = needle.lower()
    matched = [r for r in roles if key in r["title"].lower() or key in r.get("title", "")]
    if not matched:
        files = sorted((ROOT / "data" / "roles").glob("*.json"))
        matched = []
        for path in files:
            if key in path.stem.lower():
                with path.open(encoding="utf-8") as fh:
                    matched.append(json.load(fh))
    return matched


def _print_github_hit(user: dict, *, repos_preview, skip: bool, reason: str, scored: dict | None) -> None:
    login = user.get("login", "?")
    print(f"  GitHub @{login}")
    print(f"    {user.get('html_url')}")
    print(f"    loc={user.get('location') or '—'} | followers={user.get('followers', 0)} | repos={user.get('public_repos', 0)}")
    bio = (user.get("bio") or "").strip().replace("\n", " ")
    if bio:
        print(f"    bio: {bio[:120]}{'…' if len(bio) > 120 else ''}")
    if repos_preview is not None:
        owned = sum(1 for r in repos_preview if not r.get("fork"))
        print(f"    owned non-fork repos (preview): {owned}")
    if skip:
        print(f"    → SKIP hard_filter: {reason}")
    elif scored:
        print(f"    → SCORE {scored.get('score')}: {scored.get('top_signal')}")
        print(f"       {scored.get('reasoning')}")
    else:
        print("    → PASS hard_filter (not scored)")


def run_test(
    *,
    role_needle: str | None,
    max_per_query: int,
    threshold: int,
    dry_run: bool,
    search_only: bool,
) -> int:
    if not search_only and not engine.CLAUDE_API_KEY and not engine.DEEPSEEK_API_KEY:
        print("ERROR: Set CLAUDE_API_KEY or DS_API_KEY for scoring, or pass --search-only")
        return 2

    engine.MAX_CANDIDATES_PER_QUERY = max_per_query
    engine.SCORE_THRESHOLD = threshold

    roles = _pick_roles(role_needle)
    if not roles:
        print(f"No roles matched: {role_needle!r}")
        return 1

    feedback = engine.load_all_feedback_entries()
    reject_keys = engine.rejected_dedup_keys(feedback)
    seen: dict = {} if dry_run else engine.load_seen_candidates()

    print(f"Mode: {'search-only' if search_only else 'search+score'} | dry_run={dry_run} | threshold>={threshold} | max/query={max_per_query}")
    print(f"Roles: {', '.join(r['title'] for r in roles)}\n")

    all_new: list[engine.Candidate] = []
    for role in roles:
        print(f"{'=' * 60}")
        print(f"ROLE: {role['title']}")
        print(f"{'=' * 60}")
        feedback_tail = engine.format_feedback_for_prompt(role["title"], feedback)

        gh_locations = list(role.get("locations") or ([role["location"]] if role.get("location") else [None]))
        for query in role.get("github_queries", []):
            for loc in gh_locations:
                print(f"\n[GitHub] query: {query!r} + location:{loc}")
                for user in engine.search_github(query, role.get("language"), loc):
                    key = f"github:{user.get('id')}"
                    if key in seen:
                        print(f"  GitHub @{user.get('login')} — already in database")
                        continue
                    if key in reject_keys:
                        print(f"  GitHub @{user.get('login')} — skip (feedback reject)")
                        continue
                    login = (user.get("login") or "").strip()
                    repos_preview = None
                    if login:
                        repos_preview = fetch_owned_repos_preview(
                            login, headers=github_auth_headers(engine.GITHUB_TOKEN), timeout=engine.HTTP_TIMEOUT
                        )
                    skip, reason = hard_filter_github(user, repos_preview)
                    scored = None
                    if not skip and not search_only:
                        blob = (
                            f"Name: {user.get('name') or user.get('login')}\n"
                            f"Bio: {user.get('bio') or '—'}\n"
                            f"Location: {user.get('location') or '—'}\n"
                            f"Company: {user.get('company') or '—'}\n"
                            f"Public repos: {user.get('public_repos', 0)}\n"
                            f"Followers: {user.get('followers', 0)}\n"
                            f"Profile: {user.get('html_url')}"
                        )
                        scored = engine.score_candidate(role, blob, feedback_tail=feedback_tail)
                    _print_github_hit(user, repos_preview=repos_preview, skip=skip, reason=reason, scored=scored)
                    if not skip and scored and scored.get("score", 0) >= threshold:
                        cand = engine.Candidate(
                            source="github",
                            source_id=str(user.get("id")),
                            name=user.get("name") or user.get("login", "unknown"),
                            profile_url=user.get("html_url", ""),
                            role=role["title"],
                            score=int(scored["score"]),
                            reasoning=scored.get("reasoning", ""),
                            signals={"top_signal": scored.get("top_signal", "")},
                        )
                        all_new.append(cand)
                        if not dry_run:
                            seen[key] = engine.asdict(cand)

        for query in role.get("hn_queries", []):
            print(f"\n[HN] query: {query!r}")
            for hit in engine.search_hackernews(query):
                author = hit.get("author")
                if not author:
                    continue
                key = f"hackernews:{author}"
                if key in seen:
                    print(f"  HN @{author} — already in database")
                    continue
                skip, reason = hard_filter_hackernews(hit)
                scored = None
                if not skip and not search_only:
                    blob = (
                        f"HN handle: {author}\n"
                        f"Story title: {hit.get('story_title') or hit.get('title') or '—'}\n"
                        f"Comment / text: {(hit.get('comment_text') or hit.get('story_text') or '')[:600]}\n"
                        f"URL: https://news.ycombinator.com/user?id={author}"
                    )
                    scored = engine.score_candidate(role, blob, feedback_tail=feedback_tail)
                print(f"  HN @{author} — {hit.get('story_title') or hit.get('title') or 'comment'}")
                if skip:
                    print(f"    → SKIP hard_filter: {reason}")
                elif scored:
                    print(f"    → SCORE {scored.get('score')}: {scored.get('reasoning')}")
                else:
                    print("    → PASS hard_filter (not scored)")
                if not skip and scored and scored.get("score", 0) >= threshold:
                    all_new.append(engine.Candidate(
                        source="hackernews",
                        source_id=author,
                        name=author,
                        profile_url=f"https://news.ycombinator.com/user?id={author}",
                        role=role["title"],
                        score=int(scored["score"]),
                        reasoning=scored.get("reasoning", ""),
                        signals={"top_signal": scored.get("top_signal", "")},
                    ))

    print(f"\n{'=' * 60}")
    print(f"SUMMARY: {len(all_new)} candidate(s) at score >= {threshold}")
    for c in sorted(all_new, key=lambda x: x.score, reverse=True):
        print(f"  {c.score:3d} | {c.role[:40]:40s} | {c.name} | {c.profile_url}")

    if all_new and not dry_run:
        engine.save_candidates(seen)
        print(f"\nSaved to {engine.DATA_FILE}")
    elif dry_run:
        print("\n(dry-run — nothing written to data/candidates.json)")

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Test Conviva Signal sourcing accuracy for selected role(s).")
    p.add_argument("--role", help="Substring match on role title or data/roles/*.json stem")
    p.add_argument("--max-per-query", type=int, default=5, help="Hits per GitHub/HN query (default 5)")
    p.add_argument("--threshold", type=int, default=60, help="Min score to count as qualified (default 60)")
    p.add_argument("--dry-run", action="store_true", help="Do not write data/candidates.json")
    p.add_argument("--search-only", action="store_true", help="GitHub/HN + hard filters only; no LLM scoring")
    args = p.parse_args()
    return run_test(
        role_needle=args.role,
        max_per_query=args.max_per_query,
        threshold=args.threshold,
        dry_run=args.dry_run or args.search_only,
        search_only=args.search_only,
    )


if __name__ == "__main__":
    sys.exit(main())
