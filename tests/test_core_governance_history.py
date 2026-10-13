"""Phase 1 tests: trust meet, governance store, signed operator channel, hash-chained log."""

import json
import os
import stat

from correctable_agency.core.operational_state_types import (
    AuthorityGrant, Commitment, Provenance, SourceClass, trust_meet,
)
from correctable_agency.governance.governance_state_store import GovernanceStateStore
from correctable_agency.governance.signed_operator_channel import (
    OperatorSigner, OperatorVerifier, SignedMessage, load_or_create_key,
)
from correctable_agency.history.hash_chained_event_log import HashChainedEventLog

KEY = b"k" * 32
ACTOR = Provenance(SourceClass.ACTOR, "turn-1")


def test_trust_meet_takes_least_trusted():
    assert trust_meet(SourceClass.OPERATOR_SIGNED, SourceClass.ACTOR) == SourceClass.ACTOR
    assert trust_meet(SourceClass.EXTERNAL, SourceClass.BROKER) == SourceClass.EXTERNAL
    assert trust_meet(SourceClass.TOOL_OUTPUT, SourceClass.TOOL_OUTPUT) == SourceClass.TOOL_OUTPUT


def test_store_roundtrip_and_reopen(tmp_path):
    db = tmp_path / "gov.sqlite"
    s = GovernanceStateStore(db)
    c = Commitment("deploy-v2", "deploy v2 if validation passes", "release v2", 1, "release-manager", "governance")
    s.put_commitment(c)
    s.put_grant(AuthorityGrant("release-manager", frozenset({"pause"})))
    s.set_flag("paused:deploy", "1")
    s.close()
    s = GovernanceStateStore(db)
    assert s.get_commitment("deploy-v2") == c
    assert s.has_right("release-manager", "pause") and not s.has_right("actor", "pause")
    assert s.flag("paused:deploy") == "1" and s.flag("missing", "0") == "0"


def test_signed_message_accepted_once():
    m = OperatorSigner(KEY, "release-manager").sign("pause deploy")
    v = OperatorVerifier(KEY)
    assert v.verify(m) == (True, "ok")
    assert v.verify(m)[0] is False  # replay of the same message


def test_forged_and_tampered_messages_rejected():
    m = OperatorSigner(KEY, "release-manager").sign("pause deploy")
    forged = OperatorSigner(b"x" * 32, "release-manager").sign("pause deploy")
    tampered = SignedMessage(m.sender, m.seq, m.nonce, "resume deploy", m.mac)
    v = OperatorVerifier(KEY)
    assert v.verify(forged) == (False, "bad mac")
    assert v.verify(tampered) == (False, "bad mac")


def test_stale_sequence_rejected():
    signer = OperatorSigner(KEY, "release-manager")
    first, second = signer.sign("a"), signer.sign("b")
    v = OperatorVerifier(KEY)
    assert v.verify(second)[0] and v.verify(first) == (False, "stale sequence")


def test_key_created_private(tmp_path):
    path = tmp_path / "sub" / "op.key"
    key = load_or_create_key(path)
    assert len(key) == 32 and load_or_create_key(path) == key
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_log_appends_and_verifies(tmp_path):
    log = HashChainedEventLog(tmp_path / "h.jsonl", clock=lambda: 1.0)
    for i in range(5):
        log.append("model_output", ACTOR, {"i": i})
    evs = list(log.events())
    assert [e.seq for e in evs] == [1, 2, 3, 4, 5]
    assert evs[1].prev_hash == evs[0].hash
    assert log.verify() == (True, None)
    # a second handle continues the same chain
    HashChainedEventLog(tmp_path / "h.jsonl").append("verdict", ACTOR, {})
    assert log.verify() == (True, None)


def _rewrite(path, fn):
    lines = path.read_text().splitlines()
    path.write_text("\n".join(fn(lines)) + "\n")


def test_log_detects_edit_delete_reorder(tmp_path):
    p = tmp_path / "h.jsonl"
    log = HashChainedEventLog(p, clock=lambda: 1.0)
    for i in range(4):
        log.append("operator_msg", ACTOR, {"i": i})
    original = p.read_text()

    def edit(lines):
        rec = json.loads(lines[1])
        rec["payload"] = {"i": 99}
        lines[1] = json.dumps(rec, sort_keys=True)
        return lines

    _rewrite(p, edit)
    assert log.verify() == (False, 2)
    p.write_text(original)
    _rewrite(p, lambda ls: ls[:1] + ls[2:])  # delete event 2
    assert log.verify() == (False, 3)
    p.write_text(original)
    _rewrite(p, lambda ls: [ls[0], ls[2], ls[1], ls[3]])  # swap 2 and 3
    assert log.verify() == (False, 3)


def test_log_survives_lone_surrogates(tmp_path):
    log = HashChainedEventLog(tmp_path / "h.jsonl")
    log.append("model_output", ACTOR, {"text": "bad \ud800 text", "args": ["\udfff"]})
    ev = next(log.events())
    assert "\ufffd" in ev.payload["text"] or "?" in ev.payload["text"]
    assert log.verify() == (True, None)
