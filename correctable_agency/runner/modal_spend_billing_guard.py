"""Billing guard for a Modal-served model: stop submitting episodes before the cap.

Spend is projected conservatively from wall time since the endpoint was first
woken (boot is billed), plus the idle scale-down window that is billed after the
last request, plus a reserve for the episodes already in flight. Rates come from
modal.com/pricing (A100-80GB $0.000694/s, 2 GPUs ~ $5.00/h); the default adds 10%.
The guard only decides whether to *start* more episodes: it never kills one.
"""

import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


@dataclass
class ModalSpendGuard:
    cap_usd: float
    usd_per_hour: float = 5.5
    idle_minutes: float = 10.0       # scaledown_window of the deployed app
    reserve_minutes: float = 10.0    # time to drain episodes already in flight
    start: float = field(default_factory=time.time)

    def spent_usd(self) -> float:
        return (time.time() - self.start) / 3600 * self.usd_per_hour

    def projected_usd(self) -> float:
        return self.spent_usd() + (self.idle_minutes + self.reserve_minutes) / 60 * self.usd_per_hour

    def __call__(self) -> bool:
        return self.projected_usd() <= self.cap_usd


def wait_until_ready(base_url: str, timeout_s: float = 1800, poll_s: float = 15) -> float:
    """Poll /v1/models until the server answers (cold boot can take 10-20 min). Returns seconds waited."""
    t0 = time.time()
    url = base_url.rstrip("/") + "/v1/models"
    while time.time() - t0 < timeout_s:
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                if resp.status == 200:
                    return time.time() - t0
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(poll_s)
    raise TimeoutError(f"{url} not ready after {timeout_s:.0f} s")
