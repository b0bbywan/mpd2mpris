"""Pytest setup: force the C locale so tests assert against the
untranslated msgids regardless of the developer's ``$LANG``.

``mpdris2.cli`` binds the ``mpdris2`` textdomain at import time, which
``test_cli.py`` triggers transitively for the rest of the suite. Without
this guard, every ``_("Unknown title")`` in ``bridge.py`` /
``notify.py`` would resolve to the locale's translation and break the
English-string assertions.
"""

from __future__ import annotations

import os

os.environ["LANGUAGE"] = "C"
