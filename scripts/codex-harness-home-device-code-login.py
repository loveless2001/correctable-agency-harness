"""Give the GPT provider its own ChatGPT login: a device-code sign-in into a dedicated CODEX_HOME (~/.codex-harness).

The harness never touches the user's ~/.codex: its own login means its own refresh token, so token refreshes during
long grids cannot log out the user's Codex. The directory holds no config.toml, AGENTS.md or MCP servers. Starting
app-server here also installs Codex's system skills, which the provider then disables by path.

Prints the verification URL and one-time code, waits for the sign-in (15 min), then prints the plan type.
usage: codex-harness-home-device-code-login.py [--codex-home DIR]
"""

import argparse
import os
import tempfile
import time
from pathlib import Path

from correctable_agency.models.codex_app_server_json_rpc_client import DEFAULT_HOME, AppServer, app_server_argv


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--codex-home", type=Path, default=DEFAULT_HOME)
    args = p.parse_args()
    args.codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
    env["CODEX_HOME"] = str(args.codex_home)
    server = AppServer(app_server_argv(args.codex_home), env, tempfile.mkdtemp(prefix="agent-"))
    events = server.subscribe("")  # account notifications carry no threadId
    account = server.request("account/read", {}).get("account")
    if account and account.get("type") == "chatgpt":
        print(f"already signed in: plan {account.get('planType')}")
        return
    login = server.request("account/login/start", {"type": "chatgptDeviceCode"})
    print(f"open {login['verificationUrl']} and enter the code {login['userCode']}", flush=True)
    deadline = time.time() + 900
    while time.time() < deadline:
        m = events.get(timeout=max(1.0, deadline - time.time()))
        if m and m.get("method") == "account/login/completed":
            ok = (m.get("params") or {}).get("success")
            print("signed in" if ok else f"sign-in failed: {(m.get('params') or {}).get('error')}", flush=True)
            break
    account = server.request("account/read", {}).get("account") or {}
    print(f"account type {account.get('type')}, plan {account.get('planType')}")
    server.close()


if __name__ == "__main__":
    main()
