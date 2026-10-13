"""Episode memory: summary of older events + recent events in full.

- Visible events: H events whose kind is not harness-internal (`audit_*`).
- The last events that fit in `recent_bytes` are shown raw (each capped).
- Older events are shown as the fold of the summary tree within `summary_bytes`.
- Compression is lazy and in history order: only nodes the fold wants to show
  get written, so short episodes make no luna calls at all.
- If luna fails, a node gets a deterministic fallback line (marked) and an
  `audit_memory_fallback` event is logged, so the run continues (memory is
  best-effort; only science/billing guards stop runs).
"""

from pathlib import Path

from correctable_agency.core.operational_state_types import Provenance, SourceClass
from correctable_agency.history.hash_chained_event_log import Event, HashChainedEventLog
from correctable_agency.memory import luna_summary_compressor_prompts as prompts
from correctable_agency.memory.luna_summary_compressor import LunaCompressor
from correctable_agency.memory.summary_tree_index import NODE_BYTES, SummaryTree, event_line, event_text, fits
from correctable_agency.memory.summary_tree_views import context_lines, fold_parts, render_parts

CONTEXT_BYTES = 32000
RECENT_EVENT_CHARS = 4000


def visible(ev: Event) -> bool:
    return not ev.kind.startswith("audit_")


class EpisodeMemory:
    def __init__(self, log: HashChainedEventLog, tree_path: Path | None, compressor: LunaCompressor,
                 summary_bytes: int = 8000, recent_bytes: int = 24000):
        self._log, self._compressor = log, compressor
        self.tree = SummaryTree(tree_path)
        self._summary_bytes, self._recent_bytes = summary_bytes, recent_bytes

    def _events(self) -> list[Event]:
        return [ev for ev in self._log.events() if visible(ev)]

    def _fallback(self, k: int, j: int, text: str, reason: str) -> None:
        line = "(compression unavailable) " + text.encode()[: NODE_BYTES - 40].decode("utf-8", "ignore")
        self.tree.set_text(k, j, line, fallback=True)
        node = self.tree.levels[k][j]
        self._log.append("audit_memory_fallback", Provenance(SourceClass.BROKER, f"{node['lo']}-{node['hi']}"),
                         {"level": k, "reason": reason})

    def _write(self, k: int, j: int, by_seq: dict[int, Event]) -> None:
        """Write node (k, j), writing unwritten children first (history order)."""
        node = self.tree.levels[k][j]
        if node["text"] is not None:
            return
        if k == 0:
            ev = by_seq[node["lo"]]
            prompt = prompts.leaf_prompt(context_lines(self.tree, j, CONTEXT_BYTES), ev.source, ev.kind,
                                         event_text(ev))
            line = self._compressor.compress(prompt)
            if line is None:
                return self._fallback(k, j, event_line(ev), "leaf compression failed")
            return self.tree.set_text(k, j, line)
        for c in (2 * j, 2 * j + 1):
            self._write(k - 1, c, by_seq)
        a, b = self.tree.levels[k - 1][2 * j]["text"], self.tree.levels[k - 1][2 * j + 1]["text"]
        if fits(f"{a}\n{b}"):
            return self.tree.set_text(k, j, f"{a}\n{b}")
        leaf_end = (j + 1) << k
        line = self._compressor.compress(prompts.merge_prompt(context_lines(self.tree, leaf_end, CONTEXT_BYTES), [a, b]))
        if line is None:
            return self._fallback(k, j, f"{a} {b}", "merge compression failed")
        self.tree.set_text(k, j, line)

    def _split(self, events: list[Event]) -> int:
        """Index of the first event shown raw: the newest events within recent_bytes."""
        used, i = 0, len(events)
        while i > 0:
            cost = len(event_line(events[i - 1])[:RECENT_EVENT_CHARS].encode()) + 1
            if used + cost > self._recent_bytes:
                break
            used, i = used + cost, i - 1
        return i

    def render(self) -> tuple[list[str], list[str]]:
        """(summary lines for older events, raw lines for recent events)."""
        events = self._events()
        self.tree.sync(events)
        by_seq = {ev.seq: ev for ev in events}
        split = self._split(events)
        while True:
            parts = fold_parts(self.tree, split, self._summary_bytes)
            missing = [(k, j) for k, j in parts if self.tree.levels[k][j]["text"] is None]
            if not missing:
                break
            self._write(*missing[0], by_seq)
        recent = [f"#{ev.seq} " + event_line(ev)[:RECENT_EVENT_CHARS] for ev in events[split:]]
        return render_parts(self.tree, parts), recent
