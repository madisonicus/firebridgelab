#!/usr/bin/env python3
"""Checkout convenience wrapper for ``firebridgelab-fetch-data``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from firebridgelab.fetch import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
