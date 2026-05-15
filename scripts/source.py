"""Thin entry point for GitHub Actions / local runs.

Canonical implementation: `pipeline.engine`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.engine import main

if __name__ == "__main__":
    sys.exit(main())
