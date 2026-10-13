"""Each law must actually bite: a policy mutated to violate it must fail the proof gate."""

import shutil
import subprocess
from pathlib import Path

import pytest

from correctable_agency.broker.bend_policy_build import POLICY_DIR, bend_binary

needs_bend = pytest.mark.skipif(not Path(bend_binary()).exists(), reason="bend missing")

DECIDE_GOV_WRITE = "            case True{}:\n              Deny{}"
GATE = "def paused_gate(paused: Bool) -> Verdict:\n  match paused:\n    case False{}:\n      Allow{}\n    case True{}:\n      Deny{}"
OTHER = "    case Other{}:\n      Escalate{}\n\ndef decide("
READ = "    case Read{}:\n      Allow{}\n    case Edit{}:\n      Allow{}"
INV_GATE = ("def inv_gate(paused: Bool) -> Verdict:\n  match paused:\n    case False{}:\n      Deny{}\n"
            "    case True{}:\n      Allow{}\n\ndef decide_prog(")
READ_INV = READ.replace("      Allow{}\n    case Edit", "      match st:\n        case St{paused}:\n"
                        "          inv_gate(paused)\n    case Edit", 1)

# Bend reports only the first failing law, so each mutation violates exactly one law.
MUTATIONS = {
    "no_gov_write": [(DECIDE_GOV_WRITE, DECIDE_GOV_WRITE.replace("Deny", "Allow"))],
    "pause_binds_class": [(GATE, GATE.replace("      Deny{}", "      Allow{}"))],
    # Other allowed whenever unpaused: still bound by pause, so only default_escalate breaks.
    "default_escalate": [(OTHER, OTHER.replace("      Escalate{}", "      match st:\n        case St{paused}:\n"
                                                                     "          paused_gate(paused)"))],
    # Read allowed only while paused: violates monotonicity and nothing else.
    "pause_monotone": [("def decide_prog(", INV_GATE), (READ, READ_INV)],
}


@needs_bend
@pytest.mark.parametrize("law", sorted(MUTATIONS))
def test_mutated_policy_fails_named_law(tmp_path, law):
    for f in ("Policy.bend", "LAWS.bend", "PROOF.bend"):
        shutil.copy(POLICY_DIR / f, tmp_path / f)
    src = (tmp_path / "Policy.bend").read_text()
    for old, new in MUTATIONS[law]:
        assert src.count(old) == 1, f"mutation anchor for {law} not unique"
        src = src.replace(old, new)
    (tmp_path / "Policy.bend").write_text(src)
    r = subprocess.run([bend_binary(), "PROOF.bend"], cwd=tmp_path, capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    assert "SOME PROOFS FAIL" in out and law in out, out
