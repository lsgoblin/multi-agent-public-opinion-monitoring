"""Shared test fixtures.

The process in-flight ceiling is a process-wide singleton by design: it is exactly what
makes a new task unable to bypass it. Tests therefore have to reset it between cases,
otherwise one test's occupied slots leak into the next and the suite becomes order
dependent. ``reset`` is a test-only hook; production code must never call it, because
handing a stuck worker's slot to somebody else is the one thing this design must not do.
"""

import pytest

from riskshield.capacity import PROCESS_CAPACITY


@pytest.fixture(autouse=True)
def _reset_process_capacity(monkeypatch):
    """Give every test a clean ceiling and no left-over occupancy."""
    monkeypatch.delenv("RISKSHIELD_MAX_INFLIGHT_CALLS", raising=False)
    PROCESS_CAPACITY.reset()
    yield
    PROCESS_CAPACITY.reset()
