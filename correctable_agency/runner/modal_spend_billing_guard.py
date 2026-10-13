"""Billing guard for a Modal-served model, persistent across invocations.

Spend is projected conservatively from a ledger of sessions in the run directory:
each CLI invocation is one session, billed from when it started waking the
endpoint (boot is billed) to its last heartbeat, plus the idle scale-down window
after it. A resumed run (smoke -> full grid) therefore counts every earlier
session; a crashed session is counted up to its last heartbeat plus its idle
window.

Two checks, both cheap and both persisting a heartbeat:
- allow_start(): may a new episode start? Keeps a reserve for episodes in flight.
- allow_call():  may an in-flight episode make another model call? Hard cap only.
Rates come from modal.com/pricing (A100-80GB $0.000694/s, 2 GPUs ~ $5.00/h);
the default adds 10%.
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


class ModalSpendGuard:
    def __init__(self, cap_usd: float, ledger: Path, usd_per_hour: float = 5.5,
                 idle_minutes: float = 10.0, reserve_minutes: float = 20.0):
        self.cap_usd, self.rate = cap_usd, usd_per_hour
        self.idle_h, self.reserve_h = idle_minutes / 60, reserve_minutes / 60
        self._ledger = ledger
        self._sessions = json.loads(ledger.read_text())["sessions"] if ledger.exists() else []
        now = time.time()
        self._sessions.append({"start": now, "last": now, "usd_per_hour": usd_per_hour})
        self._save()

    def _save(self) -> None:
        tmp = self._ledger.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sessions": self._sessions}, indent=1))
        os.replace(tmp, self._ledger)

    def heartbeat(self) -> None:
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
                     heartbeat=lambda: None) -> float:
    """Poll /v1/models until the server answers (a cold first boot downloads weights). Returns seconds waited."""
    t0 = time.time()
    url = base_url.rstrip("/") + "/v1/models"
    while time.time() - t0 < timeout_s:
        heartbeat()
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                if resp.status == 200:
                    return time.time() - t0
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(poll_s)
    raise TimeoutError(f"{url} not ready after {timeout_s:.0f} s")
