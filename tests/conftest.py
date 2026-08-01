#!/usr/bin/env python3
"""Test-wide safety net.

A test once reached the real GitHub Pages branch and pushed a fake video to it,
because a fixture stubbed `hosting.publish` and the code under test was later
changed to call `hosting.push` instead. The stub silently stopped covering the
path it was written for.

Rather than rely on remembering to stub every entry point, every function that
touches the network or the hosting clone is replaced globally with one that
fails the test. A test that needs one must stub it explicitly and deliberately.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def no_network(monkeypatch, request):
    """Make any unstubbed outbound call an obvious test failure."""
    if request.node.get_closest_marker("allow_network"):
        return

    import urllib.request

    import hosting

    def forbidden(name):
        def fail(*args, **kwargs):
            raise AssertionError(
                f"{name} was called for real during a test. Stub it in the "
                f"fixture — a live call here can mutate the published site."
            )

        return fail

    for attribute in (
        "push",
        "publish",
        "unpublish",
        "ensure_clone",
        "list_hosted",
        "wait_until_live",
        "_git",
        "_commit_and_push",
    ):
        monkeypatch.setattr(hosting, attribute, forbidden(f"hosting.{attribute}"))

    monkeypatch.setattr(urllib.request, "urlopen", forbidden("urllib.request.urlopen"))
