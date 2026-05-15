#!/usr/bin/env python3
"""Append one TA feedback line to feedback/entries.jsonl (JSONL).

Used to close the learning loop with scripts/source.py, which reads this file
and injects recent labels into the LLM scoring prompt.

Examples:
  python scripts/log_feedback.py --source github --source-id 68322456 \\
    --role "Principal Product Builder" --label reject --reason "Wrong seniority"

  python scripts/log_feedback.py --source hackernews --source-id vrv \\
    --role "Principal Product Builder" --label strong_fit --reason "Good screen"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FEEDBACK_FILE = ROOT / "feedback" / "entries.jsonl"

LABELS = frozenset({"reject", "strong_fit", "maybe", "bad_fit", "no"})


def main() -> int:
    p = argparse.ArgumentParser(description="Append TA feedback for Conviva Signal sourcing.")
    p.add_argument("--source", required=True, help="github | hackernews | producthunt")
    p.add_argument("--source-id", required=True, dest="source_id", help="GitHub numeric id or HN username")
    p.add_argument("--role", required=True, help="Must match roles.json title for this feedback to apply")
    p.add_argument("--label", required=True, choices=sorted(LABELS), help="TA judgement")
    p.add_argument("--reason", default="", help="Short free-text reason (shown to the model)")
    p.add_argument("--file", type=Path, default=FEEDBACK_FILE, help="Override JSONL path")
    args = p.parse_args()

    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "role": args.role.strip(),
        "source": args.source.strip().lower(),
        "source_id": str(args.source_id).strip(),
        "label": args.label.strip().lower(),
        "reason": (args.reason or "").strip(),
    }
    if rec["source"] not in {"github", "hackernews", "producthunt"}:
        print("source must be one of: github, hackernews, producthunt", file=sys.stderr)
        return 2

    args.file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with args.file.open("a", encoding="utf-8") as fh:
        fh.write(line)
    print(f"Appended to {args.file}: {rec['label']} for {rec['source']}:{rec['source_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
