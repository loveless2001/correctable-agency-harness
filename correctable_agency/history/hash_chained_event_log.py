"""Attributable history H: an append-only, hash-chained JSONL event log.

Every event stores the hash of the previous event, so editing, deleting or
reordering any line breaks verify(). H is the source of truth for metrics and
for the summary tree; summaries are views over it, never replacements.
One writer per log: appends take an exclusive flock on the file.
"""

import fcntl
import json
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

from correctable_agency.core.canonical_json import sha256_hex
from correctable_agency.core.operational_state_types import Provenance

GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class Event:
    seq: int
    ts: float
    kind: str      # e.g. 'model_output', 'proposal', 'verdict', 'executed', 'operator_msg'
    source: str    # SourceClass value
    ref: str
    payload: dict
    prev_hash: str
    hash: str


def _clean(obj):
    """Make every string encodable (a lone surrogate from a model becomes U+FFFD) so an
    append can never fail on odd model text; the stored text is what was hashed."""
    if isinstance(obj, str):
        return obj.encode("utf-8", "replace").decode("utf-8")
    if isinstance(obj, dict):
        return {_clean(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def _event_hash(fields: dict) -> str:
    return sha256_hex({k: v for k, v in fields.items() if k != "hash"})


class HashChainedEventLog:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self._path, self._clock = path, clock
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)

    def _tail(self) -> tuple[int, str]:
        """(last seq, last hash), or (0, genesis) for an empty log."""
        last = None
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = line
        if last is None:
            return 0, GENESIS_HASH
        rec = json.loads(last)
        return rec["seq"], rec["hash"]

    def append(self, kind: str, prov: Provenance, payload: dict) -> Event:
        with self._path.open("a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                seq, prev = self._tail()
                fields = {"seq": seq + 1, "ts": self._clock(), "kind": kind, "source": prov.source.value,
                          "ref": _clean(prov.ref), "payload": _clean(payload), "prev_hash": prev}
                fields["hash"] = _event_hash(fields)
                f.write(json.dumps(fields, sort_keys=True, ensure_ascii=False) + "\n")
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return Event(**fields)

    def events(self) -> Iterator[Event]:
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield Event(**json.loads(line))

    def verify(self) -> tuple[bool, int | None]:
        """(True, None) if the chain is intact, else (False, seq of first bad event)."""
        prev, expected_seq = GENESIS_HASH, 1
        for ev in self.events():
            fields = asdict(ev)
            if ev.seq != expected_seq or ev.prev_hash != prev or _event_hash(fields) != ev.hash:
                return False, ev.seq
            prev, expected_seq = ev.hash, expected_seq + 1
        return True, None
