"""Shared test fixtures and path setup for Casa tests."""

import sys
from pathlib import Path

# Ensure the Casa package root is importable
_casa_root = str(Path(__file__).resolve().parent.parent / "casa-agent" / "rootfs" / "opt" / "casa")
if _casa_root not in sys.path:
    sys.path.insert(0, _casa_root)
