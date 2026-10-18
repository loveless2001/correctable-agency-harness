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
READ_INV = READ.replace("      Allow{}\n    case Edit", "      match st:\n        case St{paused, incident}:\n"
                        "          inv_gate(paused)\n    case Edit", 1)

INCIDENT_GATE = ("def incident_gate(incident: Bool) -> Verdict:\n  match incident:\n    case True{}:\n      Allow{}\n"
                 "    case False{}:\n      Escalate{}")
READ_GATE = ("def read_gate(incident: Bool) -> Verdict:\n  match incident:\n    case False{}:\n      Allow{}\n"
             "    case True{}:\n      Escalate{}\n\ndef decide_prog(")
READ_BY_INCIDENT = READ.replace("      Allow{}\n    case Edit", "      match st:\n        case St{paused, incident}:\n"
                                "          read_gate(incident)\n    case Edit", 1)
PM_READ = ("        case P.Read{} P.Work{} _:\n          {==}\n        case P.Read{} P.Gov{} False{}:\n          {==}")
# Phase 2P
DIVERT = "    case Divert{}:\n      Escalate{}\n    case Other{}:\n      Escalate{}"
DIVERT_PAUSED = DIVERT.replace("      Escalate{}\n    case Other", "      match st:\n        case St{paused, incident}:\n"
                                "          paused_gate(paused)\n    case Other", 1)
PM_DIVERT = "        case P.Divert{} P.Work{} _:\n          h\n        case P.Divert{} P.Gov{} False{}:\n          h"
ES_DIVERT = "    case P.Divert{} P.Work{} _ _:\n      {==}\n    case P.Divert{} P.Gov{} False{} _:\n      {==}"
ES_DIVERT_SPLIT = ("    case P.Divert{} P.Work{} _ False{}:\n      {==}\n    case P.Divert{} P.Work{} _ True{}:\n      {==}\n"
                   "    case P.Divert{} P.Gov{} False{} False{}:\n      {==}\n    case P.Divert{} P.Gov{} False{} True{}:\n      {==}")
PM_DEPLOY = "        case P.Deploy{} P.Work{} _:\n          {==}\n        case P.Deploy{} P.Gov{} False{}:\n          {==}"

# Bend reports only the first failing law, so each mutation violates exactly one law.
# A mutation is (old, new) on Policy.bend, or (file, old, new) on another policy file.
MUTATIONS = {
    "no_gov_write": [(DECIDE_GOV_WRITE, DECIDE_GOV_WRITE.replace("Deny", "Allow"))],
    "pause_binds_class": [(GATE, GATE.replace("      Deny{}", "      Allow{}"))],
    # Other allowed whenever unpaused: still bound by pause, so only default_escalate breaks.
    "default_escalate": [(OTHER, OTHER.replace("      Escalate{}", "      match st:\n        case St{paused, incident}:\n"
                                                                     "          paused_gate(paused)"))],
    # Read allowed only while paused: violates monotonicity and nothing else.
    "pause_monotone": [("def decide_prog(", INV_GATE), (READ, READ_INV)],
    # Phase 2E. Stabilize allowed without a declared incident: only the trigger law breaks.
    "exception_needs_trigger": [(INCIDENT_GATE, INCIDENT_GATE.replace("      Escalate{}", "      Allow{}"))],
    # Stabilize refused even with an incident: only the usability law breaks (review F2: refusal must not pass).
    "exception_usable": [(INCIDENT_GATE, INCIDENT_GATE.replace("      Allow{}", "      Escalate{}"))],
    # Read escalates while an incident is declared (same verdict paused or not, so pause_monotone's proof takes h):
    # an incident changes a verdict outside stabilize, which only the scope law forbids.
    "exception_scope": [("def decide_prog(", READ_GATE), (READ, READ_BY_INCIDENT),
                        ("PROOF.bend", PM_READ, PM_READ.replace("{==}", "h"))],
    # Phase 2P. A diverted payment allowed whenever unpaused (the pause still binds it): only the divert law breaks
    # (the other proofs are adjusted to the mutated policy so they still check).
    "divert_never_allowed": [(DIVERT, DIVERT_PAUSED), ("PROOF.bend", PM_DIVERT, PM_DIVERT.replace("h", "{==}")),
                             ("PROOF.bend", ES_DIVERT, ES_DIVERT_SPLIT)],
    # Phase 2P. The gated action refused even when unpaused (a refuse-everything policy): only the usability law breaks.
    "gated_usable": [(GATE, GATE.replace("      Allow{}", "      Escalate{}")),
                     ("PROOF.bend", PM_DEPLOY, PM_DEPLOY.replace("{==}", "h"))],
}


@needs_bend
@pytest.mark.parametrize("law", sorted(MUTATIONS))
def test_mutated_policy_fails_named_law(tmp_path, law):
    for f in ("Policy.bend", "LAWS.bend", "PROOF.bend"):
        shutil.copy(POLICY_DIR / f, tmp_path / f)
    for m in MUTATIONS[law]:
        name, old, new = m if len(m) == 3 else ("Policy.bend", *m)
        src = (tmp_path / name).read_text()
        assert src.count(old) == 1, f"mutation anchor for {law} not unique in {name}"
        (tmp_path / name).write_text(src.replace(old, new))
    r = subprocess.run([bend_binary(), "PROOF.bend"], cwd=tmp_path, capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    assert "SOME PROOFS FAIL" in out and law in out, out
