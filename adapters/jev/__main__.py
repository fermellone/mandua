"""Executable module entrypoint for adapters.jev."""

from __future__ import annotations

import sys

from adapters.jev.cli import main

if __name__ == "__main__":
    sys.exit(main())
