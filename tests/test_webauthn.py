"""WebAuthnVerifier against a software authenticator.

``SoftAuthenticator`` builds real WebAuthn bytes (authenticatorData, COSE key,
"none" attestation, ES256 signatures over authenticatorData || SHA-256(
clientDataJSON)) so these tests exercise py_webauthn's real verification, not
a mock of it. ``tests/test_gateway_api.py`` reuses it over HTTP.
"""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, encode_cbor

from glassvault.crypto import DeviceKey, challenge_digest
from glassvault.evaluator import resolve
from glassvault.gateway import Gateway, GatewayError
from glassvault.services.webauthn_verifier import EnrolmentError, WebAuthnVerifier

RP_ID = "localhost"
ORIGIN = "http://localhost:8002"
CUSTOMER = "C001"
UP, UV, BE, BS, AT = 0x01, 0x04, 0x08, 0x10, 0x40
P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


class SoftAuthenticator:
    """A software platform authenticator holding one ES256 (P-256) passkey.

    ``counts=False`` mimics synced passkeys, which always report a counter of 0.
    """

    def __init__(self, *, counts: bool = True, synced: bool = False, credential_id: bytes | None = None):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = credential_id or os.urandom(32)
        self.counts = counts
        self.synced = synced
        self.sign_count = 0
        self.user_handle: bytes | None = None

    def cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return encode_cbor({
            1: 2,    # kty: EC2
            3: -7,   # alg: ES256
            -1: 1,   # crv: P-256
            -2: numbers.x.to_bytes(32, "big"),
            -3: numbers.y.to_bytes(32, "big"),
        })

    @staticmethod
    def _auth_data(rp_id: str, flags: int, count: int, attested: bytes = b"") -> bytes:
        return hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + count.to_bytes(4, "big") + attested

    @staticmethod
    def _client_data(kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}
        ).encode()

    def create(
        self, options: dict[str, Any], *, flags: int = UP | UV | AT,
        origin: str = ORIGIN, rp_id: str = RP_ID,
    ) -> dict[str, Any]:
        """``navigator.credentials.create()``: returns the RegistrationResponseJSON."""
        self.user_handle = base64url_to_bytes(options["user"]["id"])
        attested = (
            bytes(16)  # AAGUID: all zero, as for "none" attestation
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + self.cose_key()
        )
        backup = BE | BS if self.synced else 0
        auth_data = self._auth_data(rp_id, flags | backup, 0, attested)
        client_data = self._client_data("webauthn.create", options["challenge"], origin)
        attestation = encode_cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cid = bytes_to_base64url(self.credential_id)
        return {
            "id": cid,
            "rawId": cid,
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal"],
            },
        }

    def get(
        self, options: dict[str, Any], *, flags: int = UP | UV, origin: str = ORIGIN,
        rp_id: str = RP_ID, count: int | None = None,
    ) -> dict[str, Any]:
        """``navigator.credentials.get()``: returns the AuthenticationResponseJSON."""
        if count is None:
            if self.counts:
                self.sign_count += 1
            count = self.sign_count if self.counts else 0
        backup = BE | BS if self.synced else 0
        auth_data = self._auth_data(rp_id, flags | backup, count)
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        cid = bytes_to_base64url(self.credential_id)
        return {
            "id": cid,
            "rawId": cid,
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": bytes_to_base64url(self.user_handle) if self.user_handle else None,
            },
        }

    def sign(self, message: bytes, **kwargs: Any) -> dict[str, Any]:
        """Approve a raw challenge digest (what the browser does with the gateway's options)."""
        return self.get({"challenge": bytes_to_base64url(message)}, **kwargs)


def malleate(assertion: dict[str, Any]) -> dict[str, Any]:
    """Same signed bytes, different but still valid ECDSA signature: (r, s) -> (r, n - s)."""
    r, s = decode_dss_signature(base64url_to_bytes(assertion["response"]["signature"]))
    flipped = encode_dss_signature(r, P256_ORDER - s)
    return {**assertion, "response": {**assertion["response"], "signature": bytes_to_base64url(flipped)}}


MESSAGE = hashlib.sha256(b"challenge one").digest()
OTHER_MESSAGE = hashlib.sha256(b"challenge two").digest()


@pytest.fixture
def verifier(audit, clock) -> WebAuthnVerifier:
    return WebAuthnVerifier(rp_id=RP_ID, origin=ORIGIN, audit=audit, clock=clock)


def enrol(verifier: WebAuthnVerifier, device: SoftAuthenticator, customer: str = CUSTOMER):
    options = verifier.registration_options(customer, user_name=customer)
    return verifier.verify_registration(customer, device.create(options))


@pytest.fixture
def device(verifier) -> SoftAuthenticator:
    device = SoftAuthenticator()
    enrol(verifier, device)
    return device


def last_rejection(audit) -> str:
    return [e for e in audit.entries if e.kind == "PASSKEY_REJECTED"][-1].data["reason"]


# --- Enrolment ------------------------------------------------------------------


def test_enrolment_stores_the_passkey(verifier, audit, clock):
    device = SoftAuthenticator()
    passkey = enrol(verifier, device)

    assert passkey.credential_id == device.credential_id
    assert passkey.sign_count == 0 and passkey.backed_up is False
    assert passkey.registered_at == clock.now
    assert verifier.passkeys(CUSTOMER) == (passkey,)
    assert audit.entries[-1].kind == "PASSKEY_REGISTERED"
    assert audit.entries[-1].data["credential_id"] == bytes_to_base64url(device.credential_id)


def test_registration_options_demand_biometrics_and_hide_the_customer_id(verifier):
    options = verifier.registration_options(CUSTOMER, user_name="Tan Wei Ling")

    assert options["rp"] == {"id": RP_ID, "name": "Glass Vault"}
    assert options["authenticatorSelection"]["userVerification"] == "required"
    assert options["attestation"] == "none"
    assert {p["alg"] for p in options["pubKeyCredParams"]} >= {-7, -8}
    assert len(base64url_to_bytes(options["user"]["id"])) == 32
    assert CUSTOMER not in options["user"]["id"]


def test_user_handle_is_stable_per_customer(verifier):
    first = verifier.registration_options(CUSTOMER, user_name=CUSTOMER)["user"]["id"]
    second = verifier.registration_options(CUSTOMER, user_name=CUSTOMER)["user"]["id"]
    other = verifier.registration_options("C002", user_name="C002")["user"]["id"]
    assert first == second != other


def test_enrolment_without_user_verification_is_refused(verifier, audit):
    options = verifier.registration_options(CUSTOMER, user_name=CUSTOMER)
    with pytest.raises(EnrolmentError, match="rejected"):
        verifier.verify_registration(CUSTOMER, SoftAuthenticator().create(options, flags=UP | AT))
    assert verifier.passkeys(CUSTOMER) == ()
    assert audit.entries[-1].kind == "PASSKEY_ENROLMENT_REJECTED"


@pytest.mark.parametrize("override", [{"origin": "https://evil.example"}, {"rp_id": "evil.example"}])
def test_enrolment_from_another_site_is_refused(verifier, override):
    options = verifier.registration_options(CUSTOMER, user_name=CUSTOMER)
    with pytest.raises(EnrolmentError):
        verifier.verify_registration(CUSTOMER, SoftAuthenticator().create(options, **override))
    assert verifier.passkeys(CUSTOMER) == ()


def test_enrolment_challenge_is_single_use(verifier):
    options = verifier.registration_options(CUSTOMER, user_name=CUSTOMER)
    verifier.verify_registration(CUSTOMER, SoftAuthenticator().create(options))
    with pytest.raises(EnrolmentError, match="no registration"):
        verifier.verify_registration(CUSTOMER, SoftAuthenticator().create(options))


def test_failed_enrolment_attempt_uses_up_the_challenge(verifier):
    options = verifier.registration_options(CUSTOMER, user_name=CUSTOMER)
    with pytest.raises(EnrolmentError):
        verifier.verify_registration(CUSTOMER, SoftAuthenticator().create(options, flags=UP | AT))
    with pytest.raises(EnrolmentError, match="no registration"):
        verifier.verify_registration(CUSTOMER, SoftAuthenticator().create(options))


def test_enrolment_request_expires(verifier, clock):
    options = verifier.registration_options(CUSTOMER, user_name=CUSTOMER)
    clock.advance(minutes=6)
    with pytest.raises(EnrolmentError, match="expired"):
        verifier.verify_registration(CUSTOMER, SoftAuthenticator().create(options))


def test_enrolment_must_answer_the_latest_challenge(verifier):
    stale = verifier.registration_options(CUSTOMER, user_name=CUSTOMER)
    verifier.registration_options(CUSTOMER, user_name=CUSTOMER)
    with pytest.raises(EnrolmentError):
        verifier.verify_registration(CUSTOMER, SoftAuthenticator().create(stale))


def test_enrolment_needs_options_first(verifier):
    fake = {"challenge": bytes_to_base64url(os.urandom(32)), "user": {"id": bytes_to_base64url(b"u")}}
    with pytest.raises(EnrolmentError, match="no registration"):
        verifier.verify_registration(CUSTOMER, SoftAuthenticator().create(fake))


def test_malformed_registration_is_refused_not_crashed(verifier):
    verifier.registration_options(CUSTOMER, user_name=CUSTOMER)
    with pytest.raises(EnrolmentError):
        verifier.verify_registration(CUSTOMER, {"id": "x", "rawId": "y", "response": {}})


def test_a_passkey_cannot_be_registered_twice_or_for_someone_else(verifier, device):
    options = verifier.registration_options(CUSTOMER, user_name=CUSTOMER)
    assert options["excludeCredentials"] == [
        {"id": bytes_to_base64url(device.credential_id), "type": "public-key"}
    ]
    with pytest.raises(EnrolmentError, match="already registered"):
        verifier.verify_registration(CUSTOMER, device.create(options))

    options = verifier.registration_options("C002", user_name="C002")
    with pytest.raises(EnrolmentError, match="already registered"):
        verifier.verify_registration("C002", device.create(options))
    assert verifier.passkeys("C002") == ()


def test_synced_passkey_is_flagged_as_backed_up(verifier):
    assert enrol(verifier, SoftAuthenticator(synced=True)).backed_up is True


# --- Approval ---------------------------------------------------------------------


def test_authentication_options_ask_this_customers_passkey_to_sign_the_digest(verifier, device):
    options = verifier.authentication_options(CUSTOMER, MESSAGE)

    assert base64url_to_bytes(options["challenge"]) == MESSAGE
    assert options["rpId"] == RP_ID
    assert options["userVerification"] == "required"
    assert options["allowCredentials"] == [
        {"id": bytes_to_base64url(device.credential_id), "type": "public-key"}
    ]


def test_valid_assertion_verifies_and_advances_the_counter(verifier, device):
    assert verifier.verify(CUSTOMER, MESSAGE, device.sign(MESSAGE))
    assert verifier.passkeys(CUSTOMER)[0].sign_count == 1
    assert verifier.verify(CUSTOMER, OTHER_MESSAGE, device.sign(OTHER_MESSAGE))
    assert verifier.passkeys(CUSTOMER)[0].sign_count == 2


def test_assertion_over_a_different_challenge_is_rejected(verifier, device, audit):
    assert not verifier.verify(CUSTOMER, MESSAGE, device.sign(OTHER_MESSAGE))
    assert last_rejection(audit) == "ASSERTION_INVALID"


def test_presence_without_user_verification_is_rejected(verifier, device, audit):
    assert not verifier.verify(CUSTOMER, MESSAGE, device.sign(MESSAGE, flags=UP))
    assert last_rejection(audit) == "ASSERTION_INVALID"
    assert "verif" in [e for e in audit.entries if e.kind == "PASSKEY_REJECTED"][-1].data["detail"]


def test_counter_regression_is_rejected(verifier, device, audit):
    assert verifier.verify(CUSTOMER, MESSAGE, device.sign(MESSAGE, count=5))
    # A clone of the key that has signed fewer times than the original.
    assert not verifier.verify(CUSTOMER, OTHER_MESSAGE, device.sign(OTHER_MESSAGE, count=3))
    assert last_rejection(audit) == "COUNTER_REGRESSION"
    assert not verifier.verify(CUSTOMER, OTHER_MESSAGE, device.sign(OTHER_MESSAGE, count=0))
    assert verifier.passkeys(CUSTOMER)[0].sign_count == 5


def test_replayed_assertion_is_rejected(verifier, device, audit):
    assertion = device.sign(MESSAGE)
    assert verifier.verify(CUSTOMER, MESSAGE, assertion)
    assert not verifier.verify(CUSTOMER, MESSAGE, assertion)
    assert last_rejection(audit) == "ASSERTION_REUSED"


def test_replay_is_caught_even_for_counterless_synced_passkeys(verifier, audit):
    device = SoftAuthenticator(counts=False, synced=True)
    enrol(verifier, device)
    assertion = device.sign(MESSAGE)
    assert verifier.verify(CUSTOMER, MESSAGE, assertion)
    assert not verifier.verify(CUSTOMER, MESSAGE, assertion)
    assert not verifier.verify(CUSTOMER, MESSAGE, malleate(assertion))
    assert last_rejection(audit) == "ASSERTION_REUSED"
    # A fresh approval of a new challenge still works with a counter of 0.
    assert verifier.verify(CUSTOMER, OTHER_MESSAGE, device.sign(OTHER_MESSAGE))


def test_unenrolled_authenticator_is_rejected(verifier, device, audit):
    assert not verifier.verify(CUSTOMER, MESSAGE, SoftAuthenticator().sign(MESSAGE))
    assert last_rejection(audit) == "UNKNOWN_CREDENTIAL"


def test_forged_signature_under_the_registered_credential_id_is_rejected(verifier, device, audit):
    impostor = SoftAuthenticator(credential_id=device.credential_id)
    impostor.user_handle = device.user_handle
    assert not verifier.verify(CUSTOMER, MESSAGE, impostor.sign(MESSAGE))
    assert last_rejection(audit) == "ASSERTION_INVALID"


def test_passkey_only_approves_for_its_own_customer(verifier, device, audit):
    assert not verifier.verify("C002", MESSAGE, device.sign(MESSAGE))
    assert last_rejection(audit) == "UNKNOWN_CREDENTIAL"


def test_user_handle_must_match_the_customer(verifier, device, audit):
    device.user_handle = os.urandom(32)
    assert not verifier.verify(CUSTOMER, MESSAGE, device.sign(MESSAGE))
    assert last_rejection(audit) == "USER_HANDLE_MISMATCH"


@pytest.mark.parametrize("override", [{"origin": "https://evil.example"}, {"rp_id": "evil.example"}])
def test_assertion_from_another_site_is_rejected(verifier, device, override):
    assert not verifier.verify(CUSTOMER, MESSAGE, device.sign(MESSAGE, **override))


def test_several_origins_can_be_trusted(audit, clock):
    verifier = WebAuthnVerifier(rp_id=RP_ID, origin=[ORIGIN, "http://localhost:3000"], audit=audit, clock=clock)
    device = SoftAuthenticator()
    enrol(verifier, device)
    assert verifier.verify(CUSTOMER, MESSAGE, device.sign(MESSAGE, origin="http://localhost:3000"))
    assert not verifier.verify(CUSTOMER, OTHER_MESSAGE, device.sign(OTHER_MESSAGE, origin="http://localhost:4000"))


@pytest.mark.parametrize("proof", [
    None, b"\x00" * 64, 42, {}, "not json", {"id": "x"},
    {"id": "AA", "rawId": "AA", "type": "public-key",
     "response": {"clientDataJSON": "!!", "authenticatorData": "!!", "signature": "!!"}},
])
def test_malformed_proofs_fail_closed(verifier, device, audit, proof):
    assert verifier.verify(CUSTOMER, MESSAGE, proof) is False
    assert last_rejection(audit) in {"MALFORMED_ASSERTION", "UNKNOWN_CREDENTIAL"}


def test_tampered_authenticator_data_is_rejected(verifier, device):
    assertion = device.sign(MESSAGE)
    data = bytearray(base64url_to_bytes(assertion["response"]["authenticatorData"]))
    data[-1] ^= 0x01  # bump the counter without re-signing
    assertion["response"]["authenticatorData"] = bytes_to_base64url(bytes(data))
    assert not verifier.verify(CUSTOMER, MESSAGE, assertion)


def test_no_passkey_means_nothing_verifies(verifier):
    assert not verifier.verify(CUSTOMER, MESSAGE, SoftAuthenticator().sign(MESSAGE))


# --- Plugged into the Gateway -------------------------------------------------------


def test_webauthn_verifier_drives_the_gateway(bank, audit, clock, verifier, device, brief_plan):
    gateway = Gateway(bank, audit, verifier=verifier, clock=clock)
    plan = resolve(brief_plan, bank.view(), now=clock.now)
    challenge = gateway.issue_challenge(plan)
    options = verifier.authentication_options(CUSTOMER, challenge_digest(challenge))

    receipts = gateway.execute(plan, nonce=challenge.nonce, signature=device.get(options))

    assert [r.step_id for r in receipts] == ["t1", "t2", "t3"]
    assert bank.accounts["SAV"].balance == Decimal("11257.65")
    executed = audit.entries[-1]
    assert executed.kind == "EXECUTED"
    assert executed.data["signature"]["id"] == bytes_to_base64url(device.credential_id)
    assert audit.first_broken() is None


def test_raw_device_key_signature_is_not_a_passkey(bank, audit, clock, verifier, device, brief_plan):
    gateway = Gateway(bank, audit, verifier=verifier, clock=clock)
    plan = resolve(brief_plan, bank.view(), now=clock.now)
    challenge = gateway.issue_challenge(plan)
    forged = DeviceKey().sign(challenge_digest(challenge))
    with pytest.raises(GatewayError) as e:
        gateway.execute(plan, nonce=challenge.nonce, signature=forged)
    assert e.value.code == "SIGNATURE_INVALID"
    assert bank.accounts["SAV"].balance == Decimal("12400.00")
