"""Contract every sandbox backend implements: run a literal argv, bounded, isolated."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None  # None when killed on timeout
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool        # stdout or stderr exceeded the output cap


class SandboxExecutor(Protocol):
    def run(self, argv: tuple[str, ...], timeout_s: float = 30.0) -> ExecResult:
        """Run argv literally (never through a shell) inside the episode sandbox."""
        ...
