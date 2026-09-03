"""Validation Agent — TA CLI (English). Paste LinkedIn Education after X-Ray search.

Common:
  python scripts/validate_agent.py list
  python scripts/validate_agent.py resume 1
      → Ctrl+V paste Education text, blank line to finish

Also:
  python scripts/validate_agent.py resume 1 --education "Stanford BS CS 2019"
  python scripts/validate_agent.py resume 1 --file school.txt
  python scripts/validate_agent.py how
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.validation_agent import (
    build_identity_search_links,
    list_pending,
    load_queue,
    resume_validation,
)


def _pending_sorted() -> list[dict]:
    return sorted(
        list_pending(),
        key=lambda x: (-int(x.get("score") or 0), str(x.get("dedup_key") or "")),
    )


def _resolve_key(key_or_num: str) -> str:
    key_or_num = (key_or_num or "").strip()
    pending = _pending_sorted()
    if key_or_num.isdigit():
        idx = int(key_or_num)
        if idx < 1 or idx > len(pending):
            raise KeyError(f"Index {idx} not found. Run `list` first.")
        return str(pending[idx - 1].get("dedup_key"))
    return key_or_num


def _print_search_links(item: dict) -> None:
    search = item.get("identity_search") or build_identity_search_links(item.get("candidate") or {})
    enrichment = item.get("enrichment") or (item.get("candidate") or {}).get("signals", {}).get(
        "enrichment"
    ) or {}
    if search.get("nickname_likely"):
        print("      NOTE: Display name looks like a NICKNAME/handle — not a legal name.")
        print("            Do not Google the nickname as a real person name alone.")
    print(f"      GitHub login: {search.get('github_login') or '—'}")

    conf = enrichment.get("confidence") or search.get("enrichment_confidence")
    clues = enrichment.get("clues") or search.get("enrichment_clues") or []
    links = enrichment.get("links") or search.get("enrichment_links") or []
    schools = enrichment.get("school_hits") or search.get("enrichment_schools") or []
    edu_emails = enrichment.get("edu_emails") or search.get("enrichment_edu_emails") or []
    if conf or clues or links:
        print(f"      Auto-enrichment: confidence={conf or 'n/a'}")
        if schools:
            print(f"        Schools: {', '.join(schools)}")
        if edu_emails:
            print(f"        Edu emails: {', '.join(edu_emails)}")
        for c in clues[:8]:
            print(f"        · {c}")
        if links:
            print("      Enrichment links (check these first):")
            for lk in links[:8]:
                print(f"        • {lk.get('label')}: {lk.get('url')}")

    print(f"      Tip: {search.get('tip')}")
    print("      LinkedIn X-Ray (if enrichment insufficient):")
    for s in search.get("searches") or []:
        print(f"        • {s.get('label')}")
        print(f"          {s.get('url')}")


def _read_education_interactive(candidate_name: str) -> str:
    print()
    print("=" * 60)
    print(f"  Paste school / Education text for: {candidate_name}")
    print("=" * 60)
    print()
    print("Where to copy from:")
    print("  1. Use the Google/LinkedIn X-Ray links from `list` (especially GitHub login)")
    print("  2. Open the LinkedIn profile → Education section")
    print("  3. Select text → Ctrl+C")
    print()
    print("If you cannot find LinkedIn (nickname-only GitHub):")
    print("  Paste something honest, e.g.")
    print("  LinkedIn not found. Nickname-only GitHub. No edu email on profile.")
    print()
    print("Where to paste:")
    print("  → This terminal (Ctrl+V). Multi-line OK.")
    print("  → Finish with a blank line + Enter, or a line with END")
    print()
    print("Example Education paste:")
    print("  Stanford University")
    print("  B.S. Computer Science")
    print("  2015 – 2019")
    print()
    print("-" * 60)
    print("Paste below ↓")
    print("-" * 60)

    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip().upper() == "END":
            break
        if line.strip() == "":
            if lines:
                break
            continue
        lines.append(line)

    text = "\n".join(lines).strip()
    if not text:
        print("\nNothing received. Re-run resume and paste Education (or 'LinkedIn not found').")
    return text


def cmd_list(_: argparse.Namespace) -> int:
    pending = _pending_sorted()
    if not pending:
        print()
        print("No candidates awaiting school/identity validation.")
        print("After nightly/Actions, school-unverified hits show up here.")
        print()
        return 0

    print()
    print(f"Needs validation: {len(pending)} candidate(s)")
    print("(Agent already ran GitHub/Kaggle/SO enrichment — check clues, then LinkedIn if needed)")
    print()
    for i, item in enumerate(pending, start=1):
        cand = item.get("candidate") or {}
        name = cand.get("name") or "?"
        score = item.get("score", "?")
        role = item.get("role_title") or cand.get("role") or ""
        url = cand.get("profile_url") or ""
        key = item.get("dedup_key") or ""
        print(f"  [{i}] {name}")
        print(f"      Score: {score}  |  Role: {role}")
        print(f"      GitHub: {url}")
        print(f"      Key: {key}")
        _print_search_links(item)
        print("      Next:")
        print(f"        1) Open enrichment / X-Ray links → find Education")
        print(f"        2) python scripts/validate_agent.py resume {i}")
        print(f"        3) Ctrl+V paste Education (or 'LinkedIn not found …')")
        print()

    print("Tip: use the list index, e.g. resume 1")
    print()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        key = _resolve_key(args.key)
    except KeyError as exc:
        print(exc)
        return 1
    item = load_queue().get("pending", {}).get(key)
    if not item:
        print(f"Not in pending queue: {key}")
        return 1
    cand = item.get("candidate") or {}
    print()
    print(f"Name: {cand.get('name')}")
    print(f"Score: {item.get('score')}")
    print(f"Role: {item.get('role_title')}")
    print(f"GitHub: {cand.get('profile_url')}")
    print(f"Key: {key}")
    print(f"Agent thought: {item.get('thought', '')}")
    print(f"Ask: {item.get('question', '')}")
    _print_search_links(item)
    print()
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    try:
        key = _resolve_key(args.key)
    except KeyError as exc:
        print(exc)
        return 1

    item = load_queue().get("pending", {}).get(key)
    if not item:
        print(f"Not in pending queue: {key}")
        print("Run: python scripts/validate_agent.py list")
        return 1

    name = (item.get("candidate") or {}).get("name") or key
    education = (args.education or "").strip()

    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"File not found: {path}")
            print("Create school.txt, paste Education, save, then --file school.txt")
            return 1
        education = path.read_text(encoding="utf-8").strip()

    if not education:
        print()
        _print_search_links(item)
        education = _read_education_interactive(name)

    if not education:
        return 2

    print()
    print(f"Submitting Education text to Validation Agent for [{name}]…")
    try:
        final = resume_validation(key, education)
    except KeyError as exc:
        print(exc)
        return 1

    status = final.get("status")
    score = final.get("score")
    print()
    print("-" * 40)
    if status == "ready":
        print(f"Result: READY  score={score}")
        print("OK to prioritize for outreach.")
    elif status == "rejected":
        print(f"Result: REJECTED  score={score}")
        print("Still below bar after validation — do not prioritize.")
    elif status == "needs_validation":
        print("Result: still needs more identity/school evidence.")
    else:
        print(f"Result: {status}  score={score}")
    print(f"Agent: {final.get('thought', '')}")
    print("-" * 40)
    print()
    return 0


def cmd_how(_: argparse.Namespace) -> int:
    print(
        """
============================================================
  How to find school when GitHub has no LinkedIn (English)
============================================================

GitHub often shows ONLY a nickname — no legal name, no LinkedIn, no school.
That is expected. Do NOT search the nickname as if it were a real full name.

1) Run:  python scripts/validate_agent.py list
2) Agent already auto-enriched (GitHub blog/twitter/edu email, Kaggle, StackOverflow)
3) Open enrichment links first; use LinkedIn X-Ray only if still needed
4) If you find LinkedIn → copy Education → resume N → paste in terminal
5) If you cannot find anyone:
     resume N
     paste: LinkedIn not found. Nickname-only GitHub. Enrichment found no school.
   Agent will keep them unverified / reject for outreach priority.

Where to paste Education:
  → Terminal after `resume` (Ctrl+V), OR --education "...", OR --file school.txt
Where NOT to paste:
  → Not into the GitHub website, not only into Slack

Target schools (Yan bar): MIT, Stanford, CMU, Berkeley, UCLA, Cornell,
UIUC, Michigan, Duke (or clear equivalent evidence).
"""
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Validation Agent — find identity via X-Ray, paste LinkedIn Education",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List candidates needing validation + X-Ray links").set_defaults(
        func=cmd_list
    )
    sub.add_parser("how", help="How to find school when GitHub is nickname-only").set_defaults(
        func=cmd_how
    )

    p_show = sub.add_parser("show", help="Show one pending item")
    p_show.add_argument("key", help="List index 1 or github:123")
    p_show.set_defaults(func=cmd_show)

    p_res = sub.add_parser("resume", help="Paste Education / 'LinkedIn not found' and continue")
    p_res.add_argument("key", help="List index 1 or github:123")
    p_res.add_argument("--education", default="", help="One-line Education text (optional)")
    p_res.add_argument("--file", default="", help="Read Education from a text file")
    p_res.set_defaults(func=cmd_resume)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
