#!/usr/bin/env python3
"""Version helpers — ``mpdris2/__init__.py`` is the source of truth.

Usage:
    version.py                  print PEP 440 version
    version.py --debian         print Debian-sortable equivalent
    version.py --check-tag TAG  exit 1 if TAG doesn't match (vX prefix optional)

Parses __init__.py directly (no import), so the script works without the
package's runtime dependencies installed.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

INIT = Path(__file__).resolve().parent.parent / "mpdris2" / "__init__.py"


def read_version() -> str:
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', INIT.read_text(), re.M)
    if not m:
        sys.exit(f"could not parse __version__ from {INIT}")
    return m.group(1)


def to_debian(v: str) -> str:
    """PEP 440 prerelease (rcN/bN/aN) -> Debian-sortable (~rc.N/~beta.N/~alpha.N)
    so apt's comparator sorts prereleases below the final release.
    """
    v = re.sub(r"rc(\d+)$", r"~rc.\1", v)
    v = re.sub(r"b(\d+)$", r"~beta.\1", v)
    v = re.sub(r"a(\d+)$", r"~alpha.\1", v)
    return v


def normalize_tag(tag: str) -> str:
    """Strip the leading ``v`` and convert Debian-ish prerelease forms back
    to PEP 440 so the result is comparable with ``__init__.py``.
    """
    tag = tag.removeprefix("v")
    tag = re.sub(r"-rc\.?(\d+)$", r"rc\1", tag)
    tag = re.sub(r"-beta\.?(\d+)$", r"b\1", tag)
    tag = re.sub(r"-alpha\.?(\d+)$", r"a\1", tag)
    return tag


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--debian", action="store_true", help="print Debian-sortable version")
    g.add_argument("--check-tag", metavar="TAG", help="exit 1 if TAG doesn't match __init__.py")
    args = p.parse_args()

    v = read_version()
    if args.check_tag:
        tag = normalize_tag(args.check_tag)
        if tag != v:
            sys.exit(f"tag {args.check_tag!r} does not match __init__.py version {v!r}")
        return
    print(to_debian(v) if args.debian else v)


if __name__ == "__main__":
    main()
