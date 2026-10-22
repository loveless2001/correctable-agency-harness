"""Billing guard for a token-priced API model (Phase 2G: GLM-5.3-Flash), persistent and shareable across runs.

Spend is counted in tokens: prompt + completion (thinking included) from each response's `usage`, with cached
prompt tokens counted in full (conservative). The guard holds one or more budgets, e.g. the study-wide 100M
ledger and the phase's own allocation, each a (ledger file, cap) pair, and every check must pass all of them.
Each ledger is JSON {"used": int, "calls": int, "reserved": {call key: tokens}}. Every admission and update takes an
exclusive flock on each <ledger>.lock (in a fixed order) and re-reads the file, so concurrent invocations that share a
ledger (two phases at once) add up correctly and see each other's calls in flight (codex audit 261022, H1: the
reserves used to live in the guard object, so two invocations could both admit against the same balance).

- allow_start(): a new episode may start only if `used + workers x episode_reserve` fits under every cap, so the
  episodes already in flight can finish.
- allow_call(): a model call may start only if `used + this thread's call_reserve + the reserves of other calls in
  flight` fits, counting the reserves every invocation keeps in the shared ledger. A reserve left by a process that
  no longer exists is charged in full (`unresolved_calls`), since its call may have been billed (codex re-review
  261023). The cap is a hard bound while each call stays within its reserve; the CLI providers set no max_tokens,
  so a larger response is recorded in full and can pass the cap by its overshoot.
- record(usage): after a response, add its tokens to every ledger and release the thread's reserve. A response
  without usage is charged the full reserve. Providers also record discarded attempts (audit H1).
A ledger that cannot be read or written, or an existing one without `used`, makes the checks refuse: fail closed,
since this is a billing guard.
"""

import fcntl
import json
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path


def _read(ledger: Path) -> dict:
    return json.loads(ledger.read_text()) if ledger.exists() else {"used": 0, "calls": 0}


def _write(ledger: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=ledger.parent, prefix=".token-ledger-")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, ledger)


def _settled(data: dict) -> dict:
    """The ledger with every reserve of a process that no longer exists charged in full: its call may have been billed
    before the process died. Raises KeyError for an existing ledger without `used` (no balance to trust)."""
    used, unresolved, live = int(data["used"]), int(data.get("unresolved_calls", 0)), {}
    for key, tokens in (data.get("reserved") or {}).items():
        try:
            os.kill(int(key.split(":", 1)[0]), 0)
        except ProcessLookupError:
            used, unresolved = used + int(tokens), unresolved + 1
            continue
        except (PermissionError, ValueError):
            pass  # alive under another user, or a key we cannot parse: keep it (conservative)
        live[key] = int(tokens)
    return {**data, "used": used, "reserved": live} | ({"unresolved_calls": unresolved} if unresolved else {})


def usage_tokens(usage: dict) -> int | None:
    """Tokens billed for one response: total_tokens, else prompt + completion; None if absent."""
    if not usage:
        return None
    if usage.get("total_tokens"):
        return int(usage["total_tokens"])
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        return int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
    return None


class TokenBudgetGuard:
    def __init__(self, budgets: list[tuple[Path, int]], workers: int, call_reserve: int, episode_reserve: int):
        if not budgets or any(cap <= 0 for _, cap in budgets):
            raise ValueError("token budgets need a positive cap")
        if call_reserve <= 0 or episode_reserve < call_reserve:
            raise ValueError("call_reserve must be > 0 and episode_reserve >= call_reserve")
        self.budgets = [(Path(p), int(cap)) for p, cap in budgets]
        self.workers, self.call_reserve, self.episode_reserve = max(1, workers), call_reserve, episode_reserve
        self._lock = threading.Lock()
        self._uid = uuid.uuid4().hex[:12]  # reserve keys: pid:guard:thread, shared through the ledger
        self._broken = False  # a failed ledger write: tokens went unrecorded, so refuse everything after it
        for ledger, _ in self.budgets:
            ledger.parent.mkdir(parents=True, exist_ok=True)

    def _used(self, ledger: Path) -> int | None:
        try:
            return int(_read(ledger)["used"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _fits(self, extra: int) -> bool:
        if self._broken:
            return False
        for ledger, cap in self.budgets:
            used = self._used(ledger)
            if used is None or used + extra > cap:
                return False
        return True

    def _key(self) -> str:
        return f"{os.getpid()}:{self._uid}:{threading.get_ident()}"

    @contextmanager
    def _ledgers_locked(self):
        files = [open(ledger.with_suffix(".lock"), "w") for ledger, _ in sorted(self.budgets)]  # fixed order
        try:
            for f in files:
                fcntl.flock(f, fcntl.LOCK_EX)
            yield
        finally:
            for f in files:
                fcntl.flock(f, fcntl.LOCK_UN)
                f.close()

    def used(self) -> dict[str, int | None]:
        return {str(ledger): self._used(ledger) for ledger, _ in self.budgets}

    def allow_start(self) -> bool:
        with self._lock:
            return self._fits(self.workers * self.episode_reserve)

    def allow_call(self) -> bool:
        key = self._key()
        with self._lock, self._ledgers_locked():
            if self._broken:
                return False
            try:
                data = [(ledger, cap, _settled(_read(ledger))) for ledger, cap in self.budgets]
                fits = all(d["used"] + sum(v for k, v in d["reserved"].items() if k != key) + self.call_reserve <= cap
                           for _, cap, d in data)
                for ledger, _, d in data:
                    if fits:
                        d["reserved"][key] = self.call_reserve  # a retry of the same call replaces, never adds
                    else:
                        d["reserved"].pop(key, None)
                    _write(ledger, d)
                return fits
            except (OSError, ValueError, TypeError, KeyError):
                return False  # fail closed

    def record(self, usage: dict) -> None:
        tokens = usage_tokens(usage)
        tokens = self.call_reserve if tokens is None else tokens
        key = self._key()
        with self._lock, self._ledgers_locked():
            for ledger, _ in self.budgets:
                try:
                    data = _settled(_read(ledger))
                    data["used"] += tokens
                    data["calls"] = int(data.get("calls", 0)) + 1
                    data["reserved"].pop(key, None)
                    _write(ledger, data)
                except (OSError, ValueError, TypeError, KeyError):
                    self._broken = True  # fail closed: unrecorded tokens must stop further spend
