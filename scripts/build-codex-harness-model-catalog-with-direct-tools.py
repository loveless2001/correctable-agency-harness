"""Freeze the Codex model catalog the GPT provider runs with (correctable_agency/models/codex_harness_model_catalog.json).

Source: Codex's own catalog cache (~/.codex/models_cache.json, written by the Codex CLI). Each listed GPT model keeps
its entry, with four fields changed so that Codex presents the harness tools as plain native functions:
- tool_mode "direct": no code-mode `exec` tool that wraps every tool in JavaScript (the catalog sets
  "code_mode_only" for most models);
- supports_search_tool false: no `tool_search`, which would defer tools behind a search;
- experimental_supported_tools []: no clock / async-message tools;
- multi_agent_version and multi_agent_reasoning_effort removed: no sub-agent tools.
Everything else (context window, reasoning levels, truncation) is Codex's.
"""

import argparse
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "correctable_agency/models/codex_harness_model_catalog.json"


def direct_tools_entry(model: dict) -> dict:
    m = {k: v for k, v in model.items() if k not in ("multi_agent_version", "multi_agent_reasoning_effort")}
    m.update(tool_mode="direct", supports_search_tool=False, experimental_supported_tools=[])
    return m


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--cache", type=Path, default=Path.home() / ".codex/models_cache.json")
    p.add_argument("--out", type=Path, default=OUT)
    args = p.parse_args()
    cache = json.loads(args.cache.read_text())
    models = [direct_tools_entry(m) for m in cache["models"] if m.get("visibility") == "list"]
    args.out.write_text(json.dumps({"client_version": cache.get("client_version"), "models": models}, indent=1,
                                   sort_keys=True) + "\n")
    print(f"{args.out}: {[m['slug'] for m in models]} (codex {cache.get('client_version')})")


if __name__ == "__main__":
    main()
