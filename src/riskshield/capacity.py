"""Process-wide in-flight call capacity.

Why a process-wide limit exists
-------------------------------
The engine already bounds how many calls one round may have in flight (its pool size), and
it bounds how long it waits for them. Neither bounds the *process*: a worker that never
returns still holds its thread and its connection, so starting another task simply adds a
second set of stuck workers on top. Measured before this module: four consecutive blocked
tasks at concurrency 3 left 3, 6, 9 and 12 live worker threads.

This registry is the process-wide ceiling that a new task cannot bypass, because it is not
owned by an engine, a pool or a run.

The release protocol is the point of the design
-----------------------------------------------
A slot is taken immediately before a backend call is dispatched and handed back when that
call actually returns:

* a wait timeout does **not** release the slot. Timing out only means the engine stopped
  waiting; the worker is still inside the call and the resource is still held. Releasing on
  timeout would report free capacity while the connections it stands for are gone, which is
  the same defect in a new shape;
* a late response still releases the slot, once, on the worker's own way out, so a slot can
  never leak permanently.

There is no waiting queue and no background retry. Exhaustion is reported immediately as a
structured refusal, because queuing would hide a resource shortage rather than bound it.
"""

import os
import threading
from datetime import datetime

# Configuration is read once per process. It is deliberately NOT read at import time:
# importing the package must not fail because of an environment variable, but the first
# engine entry point that could dispatch a call validates it before any call is made.
CONFIG_ENV = "RISKSHIELD_MAX_INFLIGHT_CALLS"
DEFAULT_MAX_INFLIGHT_CALLS = 16


class CapacityError(ValueError):
    """The process-wide in-flight limit is misconfigured or already exhausted."""


class InFlightCapacity:
    """A process-wide counter of backend calls that have not returned yet.

    ``used`` counts calls that were dispatched and have not returned. ``holders`` describes
    who is holding them, so the ceiling can be inspected instead of inferred from thread
    counts.
    """

    def __init__(self, limit=None):
        self._lock = threading.Lock()
        self._limit = limit
        self._used = 0
        self._holders = {}
        self._next_token = 0

    @property
    def limit(self) -> int:
        with self._lock:
            if self._limit is None:
                self._limit = configured_limit()
            return self._limit

    def used(self) -> int:
        with self._lock:
            return self._used

    def available(self) -> int:
        with self._lock:
            if self._limit is None:
                self._limit = configured_limit()
            return max(0, self._limit - self._used)

    def acquire(self, *, reason: str, run_id: str | None = None,
                round_number: int | None = None, agent_id: str | None = None) -> str:
        """Take one slot, or raise ``CapacityError`` if the process is at its ceiling.

        Never blocks: a caller that cannot be admitted is refused so the shortage stays
        visible. ``reason`` is kept in the snapshot for diagnostics only.
        """
        with self._lock:
            if self._limit is None:
                self._limit = configured_limit()
            if self._used >= self._limit:
                raise CapacityError(
                    f"process in-flight limit {self._limit} is reached; refusing to "
                    f"dispatch ({reason}) instead of queueing")
            self._used += 1
            self._next_token += 1
            token = f"slot_{self._next_token}"
            self._holders[token] = {
                "run_id": run_id, "round": round_number, "agent_id": agent_id,
                "reason": reason, "acquired_at": datetime.now().astimezone().isoformat(),
            }
            return token

    def release(self, token: str) -> bool:
        """Hand one slot back. Returns False for an unknown or already released token.

        Returning False rather than raising keeps the worker's ``finally`` path simple, but
        a False is a real signal: it means a slot was released twice or never acquired.
        """
        with self._lock:
            if token not in self._holders:
                return False
            del self._holders[token]
            self._used -= 1
            return True

    def snapshot(self) -> dict:
        with self._lock:
            if self._limit is None:
                self._limit = configured_limit()
            return {
                "limit": self._limit,
                "used": self._used,
                "available": max(0, self._limit - self._used),
                "config_source": CONFIG_ENV,
                "holders": [dict(holder) for holder in self._holders.values()],
                "note": ("used counts calls dispatched and not yet returned; a wait "
                         "timeout does not release a slot, only the backend returning "
                         "does"),
            }

    def reset(self, limit=None):
        """Test-only: clear the process ceiling and any occupancy.

        Production code must never call this: it is the one thing that could hand a stuck
        worker's slot to somebody else.
        """
        with self._lock:
            self._limit = limit
            self._used = 0
            self._holders = {}
            self._next_token = 0


def configured_limit() -> int:
    """Read and validate ``RISKSHIELD_MAX_INFLIGHT_CALLS``.

    Only a positive integer is accepted. ``0`` would refuse every call, a negative or
    non-numeric value has no meaning, and a value that Python would otherwise accept as an
    int (``True``, ``16.0``, ``"16"``) is rejected rather than silently coerced, so a typo
    cannot quietly change the ceiling.
    """
    raw = os.environ.get(CONFIG_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_INFLIGHT_CALLS
    text = raw.strip()
    if not text.isdigit() or text.startswith("+"):
        raise CapacityError(
            f"{CONFIG_ENV} must be a positive integer, got {raw!r}")
    value = int(text)
    if value < 1:
        raise CapacityError(
            f"{CONFIG_ENV} must be a positive integer, got {raw!r}")
    return value


# The single process-wide instance. Everything that dispatches a backend call shares it.
PROCESS_CAPACITY = InFlightCapacity()
