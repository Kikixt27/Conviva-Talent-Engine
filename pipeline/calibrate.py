"""Golden-set calibration (placeholder).

Planned: load `data/golden_set/*.jsonl`, replay scoring, output precision/recall.
Run: python -m pipeline.calibrate
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = ROOT / "data" / "golden_set"
SUMMARY_PATH = ROOT / "feedback" / "summary.json"


def aggregate_feedback_to_summary() -> dict:
    """Build label counts from all feedback/*.jsonl (excluding *.example)."""

    counts: dict[str, int] = {}
    total = 0
    for path in sorted((ROOT / "feedback").glob("*.jsonl")):
        if path.name.endswith(".example"):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            lab = str(row.get("label", "unknown")).lower()
            counts[lab] = counts.get(lab, 0) + 1
            total += 1
    out = {"total_labels": total, "by_label": counts}
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> int:
    if not any(GOLDEN_DIR.glob("*.jsonl")):
        print(
            "No golden set files yet. Add e.g. data/golden_set/product_builder_golden.jsonl\n"
            "See data/golden_set/*.example for schema. Writing feedback summary only.",
        )
    summary = aggregate_feedback_to_summary()
    print("Wrote", SUMMARY_PATH, summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
