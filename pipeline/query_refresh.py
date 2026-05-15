"""Weekly search-query refresh from feedback themes (placeholder).

Planned: read `feedback/summary.json` + recent JSONL reasons, suggest new
`github_queries` / `hn_queries` strings (manual review before commit).

Run: python -m pipeline.query_refresh
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "query_refresh: not implemented yet. Suggested workflow:\n"
        "  1. Review reject reasons in feedback/*.jsonl\n"
        "  2. Manually tighten roles.json or data/roles/*.json queries\n"
        "  3. Optional: call Claude once with aggregated reasons (local script).",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
