"""Run the luna compressor CLI for one summary line (adapted from agentchattr
summary_compressor_worker.py, synchronous here so episodes are reproducible).

Default command: `codex exec -m gpt-6-luna`, low effort, read-only sandbox, no
user config, ephemeral session, run in an empty directory with the prompt on
stdin. It runs on the harness side (it holds credentials), never in the episode
sandbox. Any CLI command that reads a prompt on stdin and prints one line works.
"""

import re
import shutil
import subprocess
import tempfile

from correctable_agency.memory.luna_summary_compressor_prompts import NODE_BYTES, retry_prompt

DEFAULT_COMMAND = [
    "codex", "exec", "-m", "gpt-6-luna", "-c", 'model_reasoning_effort="low"',
    "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
    "-s", "read-only", "--color", "never", "-",
]
TIMEOUT_SECONDS = 180
TRIES = 5
ACCEPT_BYTES = 600              # luna lands 5-40% over the target; this close ends the retries
MAX_LINE_BYTES = 2 * NODE_BYTES  # never cut a line; one this long counts as a failure


def flatten(raw: str) -> str:
    """CLI output as one line, without quotes wrapping the whole line."""
    text = re.sub(r"\s+", " ", raw or "").strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "`\"'":
        text = text[1:-1].strip()
    return text


class LunaCompressor:
    def __init__(self, command: list[str] | None = None):
        self._cmd = list(command or DEFAULT_COMMAND)
        self.calls = 0

    def available(self) -> bool:
        return shutil.which(self._cmd[0]) is not None

    def _run(self, prompt: str) -> str | None:
        self.calls += 1
        with tempfile.TemporaryDirectory(prefix="luna-") as cwd:
            try:
                proc = subprocess.run(self._cmd, input=prompt, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=TIMEOUT_SECONDS, cwd=cwd)
            except (OSError, subprocess.TimeoutExpired):
                return None
        return proc.stdout if proc.returncode == 0 else None

    def compress(self, prompt: str) -> str | None:
        """Shortest whole answer within MAX_LINE_BYTES over up to TRIES tries, else None."""
        tries: list[str] = []
        for _ in range(TRIES):
            line = flatten(self._run(prompt) or "")
            if not line:
                break
            if any(t.startswith(line.rstrip(" .,;:…")) for t in tries):
                continue  # an earlier try cut off: never keep cut text
            tries.append(line)
            if len(line.encode()) <= ACCEPT_BYTES:
                break
            prompt = retry_prompt(prompt, line)
        best = min(tries, key=lambda t: len(t.encode()), default=None)
        if best is None or len(best.encode()) > MAX_LINE_BYTES:
            return None
        return best
