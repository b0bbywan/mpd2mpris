"""Unit tests for the shared _http matcher. The network helpers
(``get``/``post``) are thin urllib wrappers, exercised via the per-module
tests; here we only cover the pure ``artist_matches`` logic."""

from __future__ import annotations

import pytest

from mpdris2 import _http


@pytest.mark.parametrize("query,candidate,expected", [
    ("Bob Marley", "Bob Marley", True),
    ("bob marley", "Bob  Marley!", True),            # case + punctuation folded
    ("Bob Marley", "Bob Marley & The Wailers", True),  # query ⊂ candidate
    ("Bob Marley & The Wailers", "Bob Marley", True),  # candidate ⊂ query
    ("Bob Marley", "Peter Tosh", False),
    ("", "Bob Marley", False),                       # empty never matches
    ("Bob Marley", "", False),
])
def test_artist_matches(query: str, candidate: str, expected: bool) -> None:
    assert _http.artist_matches(query, candidate) is expected
