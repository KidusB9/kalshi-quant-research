"""Shared pytest configuration.

Safety contract: a plain ``pytest`` run must ALWAYS be safe to execute —
on contributor machines and in CI — with no credentials configured.

Tests marked ``live`` talk to the real Kalshi API (some place and cancel
real orders) and/or spend real LLM credits. They are skipped unless you
explicitly opt in with BOTH the env var and the marker filter:

    RUN_LIVE_TESTS=1 pytest -m live

Never run live tests against an account whose losses you are not
prepared to accept.
"""

import os

import pytest

RUN_LIVE_TESTS = os.getenv("RUN_LIVE_TESTS", "").lower() in {"1", "true", "yes"}


def pytest_collection_modifyitems(config, items):
    if RUN_LIVE_TESTS:
        return
    skip_live = pytest.mark.skip(
        reason="live test (real Kalshi API / real money) — set RUN_LIVE_TESTS=1 and pass -m live to run"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
