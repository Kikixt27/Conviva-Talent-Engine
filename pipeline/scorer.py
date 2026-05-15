"""CLI entry: same pipeline as `python scripts/source.py`.

Usage from repository root:
    python -m pipeline.scorer
"""

from __future__ import annotations

import sys

from pipeline.engine import main

if __name__ == "__main__":
    sys.exit(main())
