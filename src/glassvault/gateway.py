"""Zero-trust execution gateway: the only component that can move money.

The agent can ask for a challenge and submit a plan, but:

- The gateway validates the plan when the challenge is issued: it must parse
  as a strict ResolvedPlan, every destination label must equal what the
  gateway itself renders from bank records, and it must simulate cleanly.
  So the customer is never asked to sign a misleading or impossible plan.
- The gateway keeps its own validated copy under the nonce and executes that
  copy, never the caller's object.
- Execution needs the customer's registered device to have signed that exact
  challenge, once, before it expires. Enrolment is write-once.

Every failure has its own code so the demo, the audit log and the benchmark
can show which defence stopped which attack.
"""

from __future__ import annotations

import hashlib
import itertools
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from .audit import AuditLog
from .bank import Bank, Market, share_cost
from .crypto import challenge_digest, plan_hash, verify_signature
from .evaluator import destination_label
from .policy import evaluate_policy
from .schema import Challenge, PolicyDecision, ResolvedPlan, ResolvedStep

PolicyFn = Callable[..., PolicyDecision]

STATUS = {
    "MALFORMED_PLAN": 400,
    "SIGNATURE_REQUIRED": 401,
    "SIGNATURE_INVALID": 401,
    "PAYLOAD_HASH_MISMATCH": 403,
    "POLICY_DENIED": 403,
    "UNKNOWN_CHALLENGE": 404,
    "DEVICE_ALREADY_REGISTERED": 409,
    "NONCE_CONSUMED": 409,
    "QUOTE_EXPIRED": 409,
    "STALE_PLAN": 409,
    "CHALLENGE_EXPIRED": 410,
    "INSUFFICIENT_FUNDS": 422,
    "POLICY_HOLD": 423,
    "INTERNAL_ERROR": 500,
}


class GatewayError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{STATUS[code]} {code}: {message}")
        self.code = code
        self.status = STATUS[code]
        self.message = message


@dataclass(frozen=True)
class Receipt:
    reference: str
    step_id: str
    summary: str


@dataclass(frozen=True)
class Execution:
    at: datetime
    step: ResolvedStep


@dataclass(frozen=True)
class _Pending:
    challenge: Challenge
    plan: ResolvedPlan  # the gateway's own validated copy


class ApprovalVerifier(Protocol):
    """Checks that `proof` is the customer's device approving `message`."""

    def verify(self, customer_id: str, message: bytes, proof: Any) -> bool: ...


class EnrollingVerifier(ApprovalVerifier, Protocol):
    """A verifier the gateway can enrol raw device keys into (write-once)."""

    def has(self, customer_id: str) -> bool: ...
    def register(self, customer_id: str, public_key: bytes) -> None: ...


class DeviceKeyVerifier:
    """Raw Ed25519 device keys (tests and CLI demo). WebAuthn plugs in the same way."""

    def __init__(self) -> None:
        self._keys: dict[str, bytes] = {}

    def has(self, customer_id: str) -> bool:
        return customer_id in self._keys

    def register(self, customer_id: str, public_key: bytes) -> None:
        self._keys[customer_id] = public_key

    def verify(self, customer_id: str, message: bytes, proof: Any) -> bool:
        key = self._keys.get(customer_id)
        return key is not None and isinstance(proof, bytes) and verify_signature(key, proof, message)


def _safe_plan_hash(plan: Any) -> str:
    try:
        return plan_hash(plan)
    except Exception:
        return "UNHASHABLE"


class Gateway:
    def __init__(
        self,
        bank: Bank,
        audit: AuditLog,
        *,
        verifier: ApprovalVerifier | None = None,
        policy: PolicyFn | None = evaluate_policy,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        challenge_ttl: timedelta = timedelta(minutes=2),
    ):
        self._bank = bank
        self._audit = audit
        self._verifier = verifier or DeviceKeyVerifier()
        # The gateway re-runs the risk rules itself: a compromised control plane cannot skip them.
        self._policy = policy
        self._clock = clock
        self._ttl = challenge_ttl
        self._lock = threading.RLock()
        self._pending: dict[str, _Pending] = {}
        self._consumed: set[str] = set()
        self._receipt_ids = itertools.count(1)
        self._executions: list[Execution] = []
        # Quotes are stamped with the gateway's clock, never a caller-supplied time.
        bank.market.bind_clock(clock)

    @property
    def bank(self) -> Bank:
        return self._bank

    @property
    def audit(self) -> AuditLog:
        return self._audit

    def now(self) -> datetime:
        return self._clock()

    def history(self, since: datetime | None = None) -> tuple[Execution, ...]:
        """Executed steps, oldest first (used by the risk rules for velocity checks)."""
        with self._lock:
            return tuple(e for e in self._executions if since is None or e.at >= since)

    def register_device(self, customer_id: str, public_key: bytes) -> None:
        """Enrol a raw Ed25519 device key. Write-once: a second enrolment is refused."""
        if not (hasattr(self._verifier, "has") and hasattr(self._verifier, "register")):
            raise TypeError("this verifier does not enrol raw device keys; enrol via the verifier itself")
        with self._lock:
            now = self._clock()
            if self._verifier.has(customer_id):
                self._audit.append(
                    "REJECTED", {"code": "DEVICE_ALREADY_REGISTERED", "customer_id": customer_id}, at=now
                )
                raise GatewayError("DEVICE_ALREADY_REGISTERED", "a device is already enrolled for this customer")
            self._verifier.register(customer_id, public_key)
            self._audit.append(
                "DEVICE_REGISTERED",
                {"customer_id": customer_id, "key_id": hashlib.sha256(public_key).hexdigest()[:16]},
                at=now,
            )

    def issue_challenge(
        self,
        plan: ResolvedPlan,
        *,
        script_hash: str | None = None,
        policy_decision_id: str | None = None,
    ) -> Challenge:
        with self._lock:
            now = self._clock()
            try:
                checked = self._check_plan(plan)
                self._simulate(checked, now)
                self._check_policy(checked, now, policy_decision_id)
            except GatewayError as e:
                self._audit.append(
                    "CHALLENGE_REFUSED", {"code": e.code, "plan_hash": _safe_plan_hash(plan)}, at=now
                )
                raise
            challenge = Challenge(
                nonce=secrets.token_hex(16),
                plan_hash=plan_hash(checked),
                customer_id=checked.customer_id,
                expires_at=now + self._ttl,
                script_hash=script_hash,
                policy_decision_id=policy_decision_id,
            )
            self._pending[challenge.nonce] = _Pending(challenge, checked)
            self._audit.append(
                "CHALLENGE_ISSUED",
                {"nonce": challenge.nonce, "plan_hash": challenge.plan_hash, "plan": checked.model_dump(mode="json")},
                at=now,
            )
            return challenge

    def pending_plan(self, nonce: str) -> ResolvedPlan | None:
        """The exact plan a pending challenge covers (what the device should display)."""
        with self._lock:
            pending = self._pending.get(nonce)
            return pending.plan if pending else None

    def execute(
        self, plan: ResolvedPlan | None = None, *, nonce: str | None = None, signature: Any = None
    ) -> list[Receipt]:
        """Execute the plan stored under `nonce` if `signature` approves it.

        `plan` is optional; if given, it must hash to what the customer approved.
        `signature` is whatever the verifier accepts: raw bytes, or a WebAuthn assertion.
        """
        with self._lock:
            try:
                return self._execute(plan, nonce, signature)
            except GatewayError as e:
                self._audit_rejection(e.code, nonce, plan)
                raise
            except Exception as e:
                self._audit_rejection("INTERNAL_ERROR", nonce, plan)
                raise GatewayError("INTERNAL_ERROR", "execution failed; nothing was moved") from e

    def _audit_rejection(self, code: str, nonce: Any, plan: Any) -> None:
        try:
            self._audit.append(
                "REJECTED",
                {"code": code, "nonce": nonce if isinstance(nonce, str) else None,
                 "plan_hash": _safe_plan_hash(plan) if plan is not None else None},
                at=self._clock(),
            )
        except Exception:
            pass  # never let an audit failure mask the original rejection

    def _execute(self, plan: ResolvedPlan | None, nonce: Any, signature: Any) -> list[Receipt]:
        now = self._clock()
        if not isinstance(nonce, str) or not nonce or not signature:
            raise GatewayError("SIGNATURE_REQUIRED", "no customer approval attached")
        if nonce in self._consumed:
            raise GatewayError("NONCE_CONSUMED", "this approval was already used")
        pending = self._pending.get(nonce)
        if pending is None:
            raise GatewayError("UNKNOWN_CHALLENGE", "no such approval request")
        challenge = pending.challenge
        if now > challenge.expires_at:
            del self._pending[nonce]
            raise GatewayError("CHALLENGE_EXPIRED", "approval request expired")
        if not self._verifier.verify(challenge.customer_id, challenge_digest(challenge), signature):
            raise GatewayError("SIGNATURE_INVALID", "signature is not from the customer's device")
        if plan is not None and _safe_plan_hash(plan) != challenge.plan_hash:
            raise GatewayError("PAYLOAD_HASH_MISMATCH", "plan differs from what the customer approved")

        # The approval is genuine: it is used up now, whether or not execution succeeds.
        del self._pending[nonce]
        self._consumed.add(nonce)

        stored = pending.plan
        new_balances = self._simulate(stored, now)
        self._check_policy(stored, now, challenge.policy_decision_id)
        receipts = [
            Receipt(f"GV{next(self._receipt_ids):08d}", s.id, f"S${s.amount:,.2f} to {s.to_display}")
            for s in stored.steps
        ]
        # Write ahead: if the audit write fails, no money has moved.
        self._audit.append(
            "EXECUTED",
            {
                "nonce": nonce,
                "plan_hash": challenge.plan_hash,
                "signature": signature.hex() if isinstance(signature, bytes) else signature,
                "receipts": [r.__dict__ for r in receipts],
            },
            at=now,
        )
        for account_id, balance in new_balances.items():
            self._bank.accounts[account_id].balance = balance
        self._executions.extend(Execution(now, s) for s in stored.steps)
        return receipts

    def _check_policy(self, plan: ResolvedPlan, now: datetime, claimed_id: str | None) -> None:
        """Deny and hold outcomes stop here, whatever the control plane said.

        `claimed_id` (the control plane's decision) is only recorded in the challenge;
        the gateway's own evaluation is what counts.
        """
        if self._policy is None:
            return
        decision = self._policy(plan, self._bank.view(), now=now, history=self.history())
        reasons = "; ".join(r.message for r in decision.reasons)
        if decision.outcome == "deny":
            raise GatewayError("POLICY_DENIED", reasons)
        if decision.outcome == "hold":
            raise GatewayError("POLICY_HOLD", reasons)

    def _check_plan(self, plan: Any) -> ResolvedPlan:
        """Strictly re-parse the plan and check every label against bank records."""
        try:
            checked = ResolvedPlan.model_validate_json(plan.model_dump_json())
        except Exception as e:  # validation, serialisation (NaN, surrogates), wrong type
            raise GatewayError("MALFORMED_PLAN", f"plan failed validation: {type(e).__name__}") from e
        if checked.customer_id != self._bank.customer_id:
            raise GatewayError("STALE_PLAN", "plan belongs to a different customer")
        for s in checked.steps:
            expected = destination_label(self._bank, s.type, s.to, s.equity)
            if expected is None or s.to_display != expected:
                raise GatewayError("STALE_PLAN", f"{s.id}: displayed destination does not match bank records")
        return checked

    def _simulate(self, plan: ResolvedPlan, now: datetime) -> dict[str, Decimal]:
        """Re-check the plan against live state; returns the balances of the accounts it touches."""
        bank = self._bank
        balances = {a.id: a.balance for a in bank.accounts.values()}
        touched: set[str] = set()
        for s in plan.steps:
            if s.from_account not in balances:
                raise GatewayError("STALE_PLAN", f"unknown account {s.from_account}")
            match s.type:
                case "own_transfer":
                    if s.to not in balances or s.to == s.from_account:
                        raise GatewayError("STALE_PLAN", f"bad destination account {s.to}")
                case "paynow":
                    if s.to not in bank.payees:
                        raise GatewayError("STALE_PLAN", f"unknown payee {s.to}")
                case "bill_payment":
                    if s.to not in bank.billers:
                        raise GatewayError("STALE_PLAN", f"unknown biller {s.to}")
                case "equity_buy":
                    fill = s.equity
                    quote = bank.market.issued(fill.quote_id) if fill else None
                    if (
                        fill is None
                        or quote is None
                        or quote.symbol != fill.symbol
                        or fill.symbol != s.to
                        or quote.price != fill.price
                        or quote.expires_at != fill.quote_expires_at
                        or share_cost(fill.price, fill.quantity) != s.amount
                    ):
                        raise GatewayError("STALE_PLAN", "equity fill does not match an issued quote")
                    if now > quote.issued_at + Market.QUOTE_TTL:
                        raise GatewayError("QUOTE_EXPIRED", f"quote {quote.quote_id} expired")
                case _:
                    raise GatewayError("STALE_PLAN", f"unsupported step type {s.type!r}")
            if balances[s.from_account] < s.amount:
                raise GatewayError("INSUFFICIENT_FUNDS", f"{s.from_account} cannot cover {s.id}")
            balances[s.from_account] -= s.amount
            touched.add(s.from_account)
            if s.type == "own_transfer":
                balances[s.to] += s.amount
                touched.add(s.to)
        # Only the accounts this plan moves money in must still match what the customer saw.
        if any(balances[a] != plan.balances_after.get(a) for a in touched):
            raise GatewayError("STALE_PLAN", "balances changed since the plan was approved")
        return {a: balances[a] for a in touched}
