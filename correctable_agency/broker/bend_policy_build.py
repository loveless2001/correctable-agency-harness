"""Proof-gated build of the broker policy.

The compiled policy.mjs is only produced after `bend PROOF.bend` prints
ALL PROOFS CHECK (and, when Lean is available or required, after the --verdict
recheck by the Lean-proven kernel). The manifest binds the hashes of the laws,
proofs, policy source and compiled module, so the bridge can refuse to run a
policy that differs from the one that was proven.

Usage: python -m correctable_agency.broker.bend_policy_build [--verdict]
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
POLICY_DIR = REPO / "broker" / "policy"
BUILD_DIR = POLICY_DIR / "build"
MANIFEST = BUILD_DIR / "manifest.json"
SOURCES = ("Policy.bend", "LAWS.bend", "PROOF.bend")
PASS = "ALL PROOFS CHECK"


def bend_binary() -> str:
    return os.environ.get("BEND", str(Path.home() / ".bend" / "bin" / "bend"))


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _run_bend(*args: str, env: dict | None = None) -> str:
    r = subprocess.run([bend_binary(), *args], cwd=POLICY_DIR, capture_output=True, text=True,
                       timeout=600, env=env)
    return r.stdout + r.stderr


def lean_available() -> bool:
    return shutil.which("lean") is not None or (Path.home() / ".elan" / "bin" / "lean").exists()


def build(verdict: bool | None = None) -> dict:
    """Check proofs, optionally recheck with the kernel, compile, write the manifest."""
    out = _run_bend("PROOF.bend")
    if PASS not in out:
        raise RuntimeError(f"proof gate failed:\n{out}")
    run_verdict = lean_available() if verdict is None else verdict
    if run_verdict:
        env = dict(os.environ)
        env["PATH"] = str(Path.home() / ".elan" / "bin") + os.pathsep + env.get("PATH", "")
        vout = _run_bend("PROOF.bend", "--verdict", env=env)
        if PASS not in vout:
            raise RuntimeError(f"--verdict kernel recheck failed:\n{vout}")
    BUILD_DIR.mkdir(exist_ok=True)
    mjs = BUILD_DIR / "policy.mjs"
    mjs.unlink(missing_ok=True)  # a stale module must not pass for a fresh compile
    cout = _run_bend("Policy.bend", "-o", str(mjs))
    if not mjs.exists():
        raise RuntimeError(f"compile failed:\n{cout}")
    manifest = {name: sha256_file(POLICY_DIR / name) for name in SOURCES}
    manifest["policy.mjs"] = sha256_file(mjs)
    manifest["verdict_kernel_checked"] = bool(run_verdict)
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def verify_manifest() -> dict:
    """Return the manifest if every hash still matches the files on disk, else raise."""
    if not MANIFEST.exists():
        raise RuntimeError("no policy manifest: run bend_policy_build first")
    manifest = json.loads(MANIFEST.read_text())
    for name in SOURCES:
        if sha256_file(POLICY_DIR / name) != manifest[name]:
            raise RuntimeError(f"{name} changed since the proven build")
    if sha256_file(BUILD_DIR / "policy.mjs") != manifest["policy.mjs"]:
        raise RuntimeError("policy.mjs differs from the proven build")
    return manifest


if __name__ == "__main__":
    print(json.dumps(build(verdict=True if "--verdict" in sys.argv else None), indent=2))
