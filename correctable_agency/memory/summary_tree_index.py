"""Binary summary tree over an episode's visible history events (OptChat-style).

Adapted from agentchattr summaries.py, minus what episodes never need (deletes,
renames, version rebuilds, backfill): H is append-only, so leaves only grow.

levels[0] holds leaves (lo == hi == the event seq); levels[k + 1][j] merges
levels[k][2j] and levels[k][2j + 1]. text=None means "not written yet".
A leaf whose line fits in NODE_BYTES is its own line, verbatim; a parent whose
two children fit together is just those lines (free nodes, no model call).

New here: every node carries `source`, the least-trusted source class among
the events it covers (trust meet). A line is never more trusted than its
weakest input, so a summary can never launder actor text into operator text.
"""

import json
import os
from pathlib import Path

from correctable_agency.core.operational_state_types import SourceClass, trust_meet
from correctable_agency.history.hash_chained_event_log import Event

NODE_BYTES = 512


def fits(text: str) -> bool:
    return len(text.encode()) <= NODE_BYTES


def event_text(ev: Event) -> str:
    """An event's content as plain text: its 'text' field, else compact JSON."""
    if isinstance(ev.payload.get("text"), str):
        return ev.payload["text"]
    return json.dumps(ev.payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def event_line(ev: Event) -> str:
    """One event on one line, whitespace folded, so text inside it cannot pose
    as another event: `<source> <kind>: <text>`."""
    return f"{ev.source} {ev.kind}: " + " ".join(event_text(ev).split())


class SummaryTree:
    def __init__(self, path: Path | None = None):
        self.levels: list[list[dict]] = []
        self._path = path
        if path and path.exists():
            self.levels = json.loads(path.read_text("utf-8"))["levels"]

    def save(self) -> None:
        if not self._path:
            return
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"levels": self.levels}, ensure_ascii=False), "utf-8")
        os.replace(tmp, self._path)

    @property
    def leaves(self) -> list[dict]:
        return self.levels[0] if self.levels else []

    def sync(self, visible_events: list[Event]) -> bool:
        """Add a leaf per new event and a parent per new complete pair; write free lines."""
        last = self.leaves[-1]["hi"] if self.leaves else 0
        fresh = [ev for ev in visible_events if ev.seq > last]
        if not fresh:
            return False
        if not self.levels:
            self.levels.append([])
        first = len(self.levels[0])
        for ev in fresh:
            line = event_line(ev)
            self.levels[0].append({"lo": ev.seq, "hi": ev.seq, "source": ev.source,
                                   "text": line if fits(line) else None})
        k = 0
        while k < len(self.levels) and len(self.levels[k]) >= 2:
            if k + 1 == len(self.levels):
                self.levels.append([])
            upper = self.levels[k + 1]
            while len(self.levels[k]) >= 2 * (len(upper) + 1):
                a, b = self.levels[k][2 * len(upper)], self.levels[k][2 * len(upper) + 1]
                src = trust_meet(SourceClass(a["source"]), SourceClass(b["source"])).value
                upper.append({"lo": a["lo"], "hi": b["hi"], "source": src, "text": None})
            k += 1
        for i in range(first, len(self.levels[0])):
            self.fill_free(0, i)
        self.save()
        return True

    def set_text(self, k: int, j: int, text: str, fallback: bool = False) -> None:
        node = self.levels[k][j]
        node["text"] = text
        if fallback:
            node["fallback"] = True
        self.fill_free(k, j)
        self.save()

    def fill_free(self, k: int, j: int) -> None:
        """Write the free ancestors of node (k, j); stop at the first that needs a model."""
        levels = self.levels
        while k + 1 < len(levels) and (j >> 1) < len(levels[k + 1]):
            parent = levels[k + 1][j >> 1]
            a, b = levels[k][j & ~1], levels[k][j | 1]
            if parent["text"] is not None or a["text"] is None or b["text"] is None:
                return
            text = f"{a['text']}\n{b['text']}"
            if not fits(text):
                return
            parent["text"] = text
            k, j = k + 1, j >> 1
