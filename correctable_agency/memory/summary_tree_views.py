"""Text views of an episode summary tree: fold rendering, compressor context, zoom, recall.

Adapted from agentchattr summary_tree_views.py. Every rendered line carries its
node's provenance tag, e.g. `#12-19 [actor] ...`, so the reader always sees the
least-trusted source behind a line.
"""

import re

from correctable_agency.history.hash_chained_event_log import Event
from correctable_agency.memory.summary_tree_fold_layout import fold
from correctable_agency.memory.summary_tree_index import NODE_BYTES, SummaryTree, event_line

OUTPUT_CHARS = 20000
SNIPPET_CHARS = 300
MAX_QUERY_CHARS = 200
PLACEHOLDER_BYTES = NODE_BYTES  # an unwritten line costs the line it stands for
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{]")


def block_id(node: dict) -> str:
    return f"#{node['lo']}" if node["lo"] == node["hi"] else f"#{node['lo']}-{node['hi']}"


def part_line(level: int, node: dict) -> str:
    text = node["text"] or (f"(not summarized yet: {1 << level} events)")
    return f"{block_id(node)} [{node['source']}] " + text.replace("\n", "\n    ")


def fold_parts(tree: SummaryTree, leaf_end: int, budget: int) -> list[tuple[int, int]]:
    """Nodes tiling leaves [0, leaf_end) within about `budget` bytes, oldest first."""
    def cost(k: int, j: int) -> tuple[int, bool]:
        node = tree.levels[k][j]
        if node["text"] is None:
            return PLACEHOLDER_BYTES, False
        return len(part_line(k, node).encode()) + 1, True
    return fold(leaf_end, budget, cost)


def render_parts(tree: SummaryTree, parts: list[tuple[int, int]]) -> list[str]:
    return [part_line(k, tree.levels[k][j]) for k, j in parts]


def context_lines(tree: SummaryTree, leaf_end: int, budget: int) -> list[str]:
    """Written lines before leaf_end, bare (no ids: compressors copy ids), for a compressor."""
    def cost(k: int, j: int) -> tuple[int, bool]:
        text = tree.levels[k][j]["text"]
        return (len(text.encode()) + 1, True) if text else (0, False)
    texts = (tree.levels[k][j]["text"] for k, j in fold(leaf_end, budget, cost))
    return [t for t in texts if t]


def zoom(tree: SummaryTree, events_by_seq: dict[int, Event], block: str) -> str:
    """Open a summary line into its two halves, or show one event whole."""
    m = re.fullmatch(r"#?(\d+)(?:-(\d+))?", (block or "").strip())
    if not m:
        return "Error: block must look like '12-19' (a summary line) or '15' (one event)."
    lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
    if lo == hi:
        ev = events_by_seq.get(lo)
        return event_line(ev)[:OUTPUT_CHARS] if ev else f"Error: no event #{lo}."
    found = next(((k, j) for k, level in enumerate(tree.levels)
                  for j, n in enumerate(level) if n["lo"] == lo and n["hi"] == hi), None)
    if found is None or found[0] == 0:
        return f"Error: no summary line #{lo}-{hi}; use ids exactly as printed."
    k, j = found
    kids = tree.levels[k - 1][2 * j:2 * j + 2]
    return "\n".join([f"#{lo}-{hi} splits into:"] + [part_line(k - 1, n) for n in kids])


def recall(events: list[Event], query: str) -> str:
    """Regex search over raw visible events (newest matches first until the cap)."""
    if not (query or "").strip():
        return "Error: query (a regex) is required."
    if len(query) > MAX_QUERY_CHARS or _NESTED_QUANTIFIER.search(query):
        return "Error: query too long or has nested quantifiers like (a+)+."
    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error as exc:
        return f"Error: bad regex: {exc}"
    out, size = [], 0
    for ev in reversed(events):
        line = event_line(ev)
        hit = pattern.search(line)
        if not hit:
            continue
        start = max(0, hit.start() - SNIPPET_CHARS // 3)
        snippet = f"#{ev.seq} " + line[start:start + SNIPPET_CHARS]
        if size + len(snippet) > OUTPUT_CHARS:
            break
        out.append(snippet)
        size += len(snippet) + 1
    return "\n".join(reversed(out)) if out else "No match."
