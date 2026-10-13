"""Canonical JSON encoding shared by everything that hashes or signs data.

Two records with the same content must yield the same bytes, so keys are sorted
and whitespace is fixed. Used by the HMAC operator channel and the event log.
"""

import hashlib
import json
from typing import Any


def canonical_bytes(obj: Any) -> bytes:
    """Deterministic UTF-8 JSON bytes for hashing/signing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(obj: Any) -> str:
    """SHA-256 hex digest of the canonical encoding."""
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()
