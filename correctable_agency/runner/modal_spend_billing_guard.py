"""Billing guard for a Modal-served model, persistent across invocations.

Spend is projected conservatively from a ledger of sessions in the run directory:
each CLI invocation is one session, billed from when it started waking the
endpoint (boot is billed) to its last heartbeat, plus the idle scale-down window
after it. A resumed run (smoke -> full grid) therefore counts every earlier
session; a crashed session is counted up to its last heartbeat plus its idle
window. One invocation per run directory at a time (an exclusive lock file).

Two checks, both thread-safe and both recording a heartbeat:
- allow_start(): may a new episode start? Keeps a reserve for episodes in flight.
- allow_call():  may an in-flight episode make another model call? Hard cap only.
The guard never raises from a check: if the ledger cannot be written, the
in-memory ledger still decides. Rates come from modal.com/pricing (A100-80GB
$0.000694/s, 2 GPUs ~ $5.00/h); the default adds 10% and anything below
MIN_USD_PER_HOUR is refused, so the guard cannot be disabled by a typo.
"""

import fcntl
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

MIN_USD_PER_HOUR = 5.0


class ModalSpendGuard:
    def __init__(self, cap_usd: float, ledger: Path, usd_per_hour: float = 5.5,
                 idle_minutes: float = 10.0, reserve_minutes: float = 20.0):
        if usd_per_hour < MIN_USD_PER_HOUR:
            raise ValueError(f"usd_per_hour {usd_per_hour} below the {MIN_USD_PER_HOUR} floor")
        self.cap_usd, self.rate = cap_usd, usd_per_hour
        self.idle_h, self.reserve_h = idle_minutes / 60, reserve_minutes / 60
        self._ledger, self._lock = ledger, threading.Lock()
        self._lockfile = open(ledger.with_suffix(".lock"), "w")
        try:
            fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RuntimeError(f"another invocation is using {ledger.parent}") from e
        self._sessions = json.loads(ledger.read_text())["sessions"] if ledger.exists() else []
        now = time.time()
        self._sessions.append({"start": now, "last": now, "usd_per_hour": usd_per_hour})
        self._save()

    def close(self) -> None:
        """Release the run-directory lock (end of the invocation)."""
        self.heartbeat()
        fcntl.flock(self._lockfile, fcntl.LOCK_UN)
        self._lockfile.close()

    def _save(self) -> None:
        """Atomic write through a unique temp file; on failure keep deciding from memory."""
        try:
            fd, tmp = tempfile.mkstemp(dir=self._ledger.parent, prefix=".ledger-")
            with os.fdopen(fd, "w") as f:
                json.dump({"sessions": self._sessions}, f, indent=1)
            os.replace(tmp, self._ledger)
        except OSError:
            pass

    def heartbeat(self) -> None:
        with self._lock:
            self._sessions[-1]["last"] = time.time()
            self._save()

    def spent_usd(self) -> float:
        """Billed so far: every session's span plus every earlier session's idle window."""
        total = 0.0
        for i, s in enumerate(self._sessions):
            hours = (s["last"] - s["start"]) / 3600 + (self.idle_h if i < len(self._sessions) - 1 else 0)
            total += hours * s["usd_per_hour"]
        return total

    def projected_usd(self, reserve: bool = True) -> float:
        """Spent + this session's idle window (+ the in-flight reserve)."""
        return self.spent_usd() + (self.idle_h + (self.reserve_h if reserve else 0)) * self.rate

    def allow_start(self) -> bool:
        self.heartbeat()
        return self.projected_usd(reserve=True) <= self.cap_usd

    def allow_call(self) -> bool:
        self.heartbeat()
        return self.projected_usd(reserve=False) <= self.cap_usd


def wait_until_ready(base_url: str, timeout_s: float = 2400, poll_s: float = 15,
                     budget_ok: Callable[[], bool] = lambda: True) -> float:
    """Poll /v1/models until the server answers (a cold first boot downloads weights).
    Each poll checks the budget, so a stuck boot cannot outspend the cap."""
    t0 = time.time()
    url = base_url.rstrip("/") + "/v1/models"
    while time.time() - t0 < timeout_s:
        if not budget_ok():
            raise RuntimeError("billing guard: cap reached while waiting for the endpoint")
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                if resp.status == 200:
                    return time.time() - t0
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(poll_s)
    raise TimeoutError(f"{url} not ready after {timeout_s:.0f} s")
