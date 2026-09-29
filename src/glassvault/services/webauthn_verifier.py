"""WebAuthn passkeys as the gateway's approval check.

The customer approves a plan with Face ID or Touch ID. The passkey does not
sign a random server challenge: it signs ``crypto.challenge_digest(challenge)``,
the SHA-256 of the canonical ``Challenge{plan_hash, nonce, customer_id,
expires_at, script_hash, policy_decision_id}``. One assertion therefore covers
exactly one plan, one nonce and one expiry, and the gateway's check keeps the
same shape as with the raw ``DeviceKey``: ``verify(customer_id, message, proof)``.

Choices worth knowing:

- User verification (biometric or device PIN) is required at enrolment and at
  every approval. Presence alone (a tap) is not consent to move money.
- Verification fails closed. Malformed input is a rejection, never an
  exception that could surface as a 500 or be mistaken for success.
- The signature is checked before the counter, so a counter regression is only
  reported for a genuinely signed assertion. That means a cloned authenticator
  or a replay, not a forgery.
- Each signed assertion is accepted once. Synced passkeys (iCloud Keychain,
  Google Password Manager) always report a counter of 0, so the counter alone
  cannot catch a replay. The gateway's nonce set is the first line; this is
  the second.
- Attestation is "none": we do not check which authenticator model made the
  key. A bank would require attestation or a bank-SDK key in the Secure Enclave.
- Credentials live in memory, and the class is not thread-safe: the gateway
  service serialises every call into it.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    bytes_to_base64url,
    options_to_json_dict,
    parse_authentication_credential_json,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..audit import AuditLog

REGISTRATION_TTL = timedelta(minutes=5)
APPROVAL_TIMEOUT_MS = 120_000  # matches the gateway's default challenge TTL
# ES256 covers Apple, Google and most security keys; Ed25519 and RS256 (Windows Hello) the rest.
ALGORITHMS = [
    COSEAlgorithmIdentifier.ECDSA_SHA_256,
    COSEAlgorithmIdentifier.EDDSA,
    COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
]


class EnrolmentError(Exception):
    """A passkey registration was refused; the message says why."""


@dataclass(frozen=True)
class Passkey:
    customer_id: str
    credential_id: bytes
    public_key: bytes  # COSE_Key, exactly as the authenticator reported it
    sign_count: int
    aaguid: str
    backed_up: bool  # synced passkey: bound to a cloud account rather than one device
    registered_at: datetime

    def summary(self) -> dict[str, Any]:
        """JSON-safe description (a public key and counter are not secrets)."""
        return {
            "customer_id": self.customer_id,
            "credential_id": bytes_to_base64url(self.credential_id),
            "aaguid": self.aaguid,
            "sign_count": self.sign_count,
            "backed_up": self.backed_up,
            "registered_at": self.registered_at.isoformat(),
        }


@dataclass(frozen=True)
class _PendingRegistration:
    challenge: bytes
    expires_at: datetime


class _Rejected(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def approval_options(
    rp_id: str,
    message: bytes,
    credential_ids: Iterable[bytes] = (),
    *,
    timeout_ms: int = APPROVAL_TIMEOUT_MS,
) -> dict[str, Any]:
    """``navigator.credentials.get()`` options whose challenge is ``message``."""
    options = generate_authentication_options(
        rp_id=rp_id,
        challenge=message,
        timeout=timeout_ms,
        allow_credentials=[PublicKeyCredentialDescriptor(id=c) for c in credential_ids],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return options_to_json_dict(options)


def _short(error: Exception) -> str:
    # Library messages can echo attacker-supplied fields (e.g. origin); keep them short.
    return f"{type(error).__name__}: {error}"[:160]


class WebAuthnVerifier:
    """Passkey enrolment plus the gateway's ``ApprovalVerifier``."""

    def __init__(
        self,
        *,
        rp_id: str,
        origin: str | Sequence[str],
        rp_name: str = "Glass Vault",
        audit: AuditLog | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.rp_id = rp_id
        self.origins: tuple[str, ...] = (origin,) if isinstance(origin, str) else tuple(origin)
        if not self.origins:
            raise ValueError("at least one origin is required")
        self.rp_name = rp_name
        self._audit = audit
        self._clock = clock
        self._user_handles: dict[str, bytes] = {}
        self._credentials: dict[str, dict[bytes, Passkey]] = {}
        self._owners: dict[bytes, str] = {}
        self._registrations: dict[str, _PendingRegistration] = {}
        self._used: set[bytes] = set()

    def passkeys(self, customer_id: str) -> tuple[Passkey, ...]:
        return tuple(self._credentials.get(customer_id, {}).values())

    # --- Enrolment ------------------------------------------------------------

    def registration_options(
        self, customer_id: str, *, user_name: str, display_name: str | None = None
    ) -> dict[str, Any]:
        """``navigator.credentials.create()`` options; starts one registration attempt."""
        # A random, stable user handle: the customer id itself never goes to the authenticator.
        handle = self._user_handles.setdefault(customer_id, secrets.token_bytes(32))
        options = generate_registration_options(
            rp_id=self.rp_id,
            rp_name=self.rp_name,
            user_id=handle,
            user_name=user_name,
            user_display_name=display_name or user_name,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=p.credential_id) for p in self.passkeys(customer_id)
            ],
            supported_pub_key_algs=ALGORITHMS,
        )
        self._registrations[customer_id] = _PendingRegistration(
            options.challenge, self._clock() + REGISTRATION_TTL
        )
        return options_to_json_dict(options)

    def verify_registration(self, customer_id: str, credential: Mapping[str, Any] | str) -> Passkey:
        """Check the browser's ``PublicKeyCredential`` and store the passkey."""
        try:
            passkey = self._register(customer_id, credential)
        except EnrolmentError as e:
            self._log("PASSKEY_ENROLMENT_REJECTED", {"customer_id": customer_id, "reason": str(e)})
            raise
        self._log("PASSKEY_REGISTERED", passkey.summary())
        return passkey

    def _register(self, customer_id: str, credential: Mapping[str, Any] | str) -> Passkey:
        # One attempt per options call: a failed ceremony must start again.
        pending = self._registrations.pop(customer_id, None)
        now = self._clock()
        if pending is None:
            raise EnrolmentError("no registration was started for this customer")
        if now > pending.expires_at:
            raise EnrolmentError("registration request expired")
        try:
            verified = verify_registration_response(
                credential=credential if isinstance(credential, str) else dict(credential),
                expected_challenge=pending.challenge,
                expected_rp_id=self.rp_id,
                expected_origin=list(self.origins),
                require_user_verification=True,
                supported_pub_key_algs=ALGORITHMS,
            )
        except Exception as e:  # fail closed on anything malformed
            raise EnrolmentError(f"registration response rejected ({_short(e)})") from e
        if verified.credential_id in self._owners:
            raise EnrolmentError("this passkey is already registered")

        passkey = Passkey(
            customer_id=customer_id,
            credential_id=verified.credential_id,
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            aaguid=verified.aaguid,
            backed_up=verified.credential_backed_up,
            registered_at=now,
        )
        self._credentials.setdefault(customer_id, {})[passkey.credential_id] = passkey
        self._owners[passkey.credential_id] = customer_id
        return passkey

    # --- Approval (gateway.ApprovalVerifier) ------------------------------------

    def authentication_options(
        self, customer_id: str, message: bytes, *, timeout_ms: int = APPROVAL_TIMEOUT_MS
    ) -> dict[str, Any]:
        """Options that ask the customer's own passkeys to sign ``message``."""
        ids = (p.credential_id for p in self.passkeys(customer_id))
        return approval_options(self.rp_id, message, ids, timeout_ms=timeout_ms)

    def verify(self, customer_id: str, message: bytes, proof: Any) -> bool:
        """True only for a fresh, user-verified assertion over ``message`` by this customer's passkey."""
        try:
            passkey, sign_count, signed = self._check(customer_id, message, proof)
        except _Rejected as r:
            self._log(
                "PASSKEY_REJECTED", {"customer_id": customer_id, "reason": r.reason, "detail": r.detail}
            )
            return False
        self._credentials[customer_id][passkey.credential_id] = replace(passkey, sign_count=sign_count)
        self._used.add(signed)
        return True

    def _check(self, customer_id: str, message: bytes, proof: Any) -> tuple[Passkey, int, bytes]:
        if not isinstance(proof, dict | str):
            raise _Rejected("MALFORMED_ASSERTION", f"expected assertion JSON, got {type(proof).__name__}")
        try:
            credential = parse_authentication_credential_json(proof)
        except Exception as e:
            raise _Rejected("MALFORMED_ASSERTION", _short(e)) from e

        passkey = self._credentials.get(customer_id, {}).get(credential.raw_id)
        if passkey is None:
            raise _Rejected("UNKNOWN_CREDENTIAL", "not a passkey registered to this customer")
        handle = credential.response.user_handle
        if handle is not None and handle != self._user_handles.get(customer_id):
            raise _Rejected("USER_HANDLE_MISMATCH", "assertion names a different user")

        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=message,
                expected_rp_id=self.rp_id,
                expected_origin=list(self.origins),
                credential_public_key=passkey.public_key,
                # 0 disables the library's counter check, which runs before the
                # signature check; ours runs after it (see the module docstring).
                credential_current_sign_count=0,
                require_user_verification=True,
            )
        except Exception as e:  # fail closed
            raise _Rejected("ASSERTION_INVALID", _short(e)) from e

        # What the authenticator signed: authenticatorData || SHA-256(clientDataJSON).
        # Keyed on the signed bytes, not the signature, because ECDSA signatures are malleable.
        response = credential.response
        signed = hashlib.sha256(
            response.authenticator_data + hashlib.sha256(response.client_data_json).digest()
        ).digest()
        if signed in self._used:
            raise _Rejected("ASSERTION_REUSED", "this exact approval was already used")
        count = verified.new_sign_count
        if (count or passkey.sign_count) and count <= passkey.sign_count:
            raise _Rejected(
                "COUNTER_REGRESSION",
                f"counter {count} after {passkey.sign_count}: cloned authenticator or replay",
            )
        return passkey, count, signed

    def _log(self, kind: str, data: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit.append(kind, data, at=self._clock())
