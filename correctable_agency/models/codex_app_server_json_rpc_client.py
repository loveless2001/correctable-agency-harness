"""JSON-RPC client for `codex app-server` (stdio, newline-delimited JSON) with every Codex extra switched off.

Used by the GPT provider (codex_app_server_provider) and the harness login / request-audit scripts. The CONFIG
overrides, disabled features and frozen model catalog are what the mock-Responses audit checks
(scripts/codex-app-server-request-audit-against-mock-responses.py): the model sees the harness system prompt, the
rendered message and the harness tools, plus Codex's `request_user_input`.
"""

import json
import os
import queue
import subprocess
import threading
from pathlib import Path

RPC_TIMEOUT_S = 60
CATALOG = Path(__file__).with_name("codex_harness_model_catalog.json")
DEFAULT_HOME = Path.home() / ".codex-harness"
CONFIG = [f'model_catalog_json="{CATALOG}"', 'web_search="disabled"', "include_environment_context=false",
          "include_collaboration_mode_instructions=false", "include_apps_instructions=false",
          "include_permissions_instructions=false", "project_doc_max_bytes=0", 'history.persistence="none"',
          "analytics.enabled=false",
          'model_verbosity="medium"']  # the Responses API default; Codex's catalog default for these models is "low"
DISABLED_FEATURES = ["multi_agent", "goals", "memories", "apps", "plugins", "skill_search", "sleep_tool",
                     "image_generation", "view_image", "browser_use", "computer_use", "tool_suggest", "hooks",
                     "shell_tool", "unified_exec", "collaboration_modes"]


def app_server_argv(codex_home: Path, codex_bin: str = "codex") -> list[str]:
    """`codex app-server` with every Codex extra off. Codex installs its system skills into CODEX_HOME; each one is
    disabled by path (the login script starts app-server once, so they exist before the first episode)."""
    system = codex_home / "skills" / ".system"
    skills = sorted(p.name for p in system.iterdir() if (p / "SKILL.md").exists()) if system.is_dir() else []
    cfg = CONFIG + ["skills.config=[" + ",".join(
        f'{{path="{system / s / "SKILL.md"}",enabled=false}}' for s in skills) + "]"]
    return [codex_bin, "app-server", "--listen", "stdio://", *[a for c in cfg for a in ("-c", c)],
            *[a for f in DISABLED_FEATURES for a in ("--disable", f)]]


class AppServerError(Exception):
    """An infrastructure failure of one attempt (app-server down, turn failed, timeout)."""


class AppServer:
    """One `codex app-server` child. Responses are matched by request id; notifications and server requests are
    routed to the queue of their threadId. Server requests for no subscribed thread are refused."""

    def __init__(self, argv: list[str], env: dict, cwd: str, stderr_path: str = os.devnull):
        self._proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=open(stderr_path, "a"), text=True, env=env, cwd=cwd)
        self._lock, self._next = threading.Lock(), 0
        self._waiting: dict[int, queue.Queue] = {}
        self._threads: dict[str, queue.Queue] = {}
        self.rate_limits: dict = {}
        threading.Thread(target=self._read, daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": "correctable_agency_harness", "title": "Correctable Agency",
                                                   "version": "1"}, "capabilities": {"experimentalApi": True}})
        self._send({"method": "initialized"})

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def _send(self, msg: dict) -> None:
        with self._lock:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()

    def _read(self) -> None:
        for line in self._proc.stdout:
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in m and "method" not in m:
                q = self._waiting.pop(m["id"], None)
                if q is not None:
                    q.put(m)
                continue
            params = m.get("params") or {}
            if m.get("method") == "account/rateLimits/updated":
                self.rate_limits = params.get("rateLimits") or {}
            q = self._threads.get(params.get("threadId") or "")
            if q is not None:
                q.put(m)
            elif "id" in m:
                self.refuse(m["id"])
        for q in [*self._waiting.values(), *self._threads.values()]:
            q.put(None)  # the process exited: wake every waiter

    def refuse(self, rid) -> None:
        self._send({"id": rid, "error": {"code": -32601, "message": "not supported by this client"}})

    def request(self, method: str, params: dict, timeout: float = RPC_TIMEOUT_S) -> dict:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._next += 1
            rid = self._next
            self._waiting[rid] = q
        self._send({"id": rid, "method": method, "params": params})
        try:
            m = q.get(timeout=timeout)
        except queue.Empty:
            raise AppServerError(f"{method}: no reply in {timeout:.0f} s") from None
        if m is None or "error" in m:
            raise AppServerError(f"{method}: {(m or {}).get('error') or 'app-server exited'}"[:500])
        return m.get("result") or {}

    def subscribe(self, thread_id: str) -> queue.Queue:
        self._threads[thread_id] = q = queue.Queue()
        return q

    def unsubscribe(self, thread_id: str) -> None:
        self._threads.pop(thread_id, None)

    def send_request_no_reply(self, method: str, params: dict) -> None:
        """A request whose reply nobody waits for (it is dropped by the reader)."""
        with self._lock:
            self._next += 1
            rid = f"noreply-{self._next}"
        self._send({"id": rid, "method": method, "params": params})

    def notify_unsubscribe(self, thread_id: str) -> None:
        """Let app-server unload the finished thread (best effort; the reply is not awaited)."""
        try:
            self._send({"id": f"unsubscribe-{thread_id}", "method": "thread/unsubscribe", "params": {"threadId": thread_id}})
        except OSError:
            pass

    def close(self) -> None:
        self._proc.kill()
        try:
            self._proc.stdin.close()  # unflushed bytes to a dead process must not raise later, at garbage collection
        except OSError:
            pass
