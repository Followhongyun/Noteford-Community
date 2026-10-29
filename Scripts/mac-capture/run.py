#!/usr/bin/env python3
"""Launch the macOS official-WeChat resign and capture entry.

Linux and Windows exit before touching WeChat. The device checklist is
docs/macos-feasibility.md section 7.8.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from noteford.mac_entry import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
