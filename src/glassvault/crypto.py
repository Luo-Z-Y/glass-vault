"""Canonical hashing and device signatures.

``DeviceKey`` stands in for the customer's passkey (Face ID / Touch ID). The
web app will use WebAuthn instead; the gateway's check stays the same shape:
a signature from the customer's registered key over the challenge digest.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import BaseModel

from .schema import Challenge, ResolvedPlan


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def canonical_bytes(obj: Any) -> bytes:
    """Stable JSON: sorted keys, no whitespace, Decimals as strings."""
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default
    ).encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def plan_hash(plan: ResolvedPlan) -> str:
    return sha256_hex(canonical_bytes(plan))


def challenge_digest(challenge: Challenge) -> bytes:
    return hashlib.sha256(canonical_bytes(challenge)).digest()


class DeviceKey:
    def __init__(self, private_key: Ed25519PrivateKey | None = None):
        self._private_key = private_key or Ed25519PrivateKey.generate()

    @property
    def public_bytes(self) -> bytes:
        return self._private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign(self, message: bytes) -> bytes:
        return self._private_key.sign(message)


def verify_signature(public_key: bytes, signature: bytes, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True
