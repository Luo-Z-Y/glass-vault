"""Zero-trust execution gateway: the only component that can move money.

The agent can ask for a challenge and submit a plan, but the gateway executes
only if the customer's registered device signed that challenge, and the plan
hashes to exactly what the challenge covers. Each failure has its own code so
the demo (and the audit log) can show which defence stopped which attack.
"""

from __future__ import annotations

import itertools
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .audit import AuditLog
from .bank import Bank
from .crypto import challenge_digest, plan_hash, verify_signature
from .schema import Challenge, ResolvedPlan

STATUS = {
    "SIGNATURE_REQUIRED": 401,
    "SIGNATURE_INVALID": 401,
    "PAYLOAD_HASH_MISMATCH": 403,
    "UNKNOWN_CHALLENGE": 404,
    "NONCE_CONSUMED": 409,
    "QUOTE_EXPIRED": 409,
    "STALE_PLAN": 409,
    "CHALLENGE_EXPIRED": 410,
    "INSUFFICIENT_FUNDS": 422,
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


class Gateway:
    def __init__(
        self,
        bank: Bank,
        audit: AuditLog,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        challenge_ttl: timedelta = timedelta(minutes=2),
    ):
        self._bank = bank
        self._audit = audit
        self._clock = clock
        self._ttl = challenge_ttl
        self._device_keys: dict[str, bytes] = {}
        self._pending: dict[str, Challenge] = {}
        self._consumed: set[str] = set()
        self._receipt_ids = itertools.count(1)

    def register_device(self, customer_id: str, public_key: bytes) -> None:
        self._device_keys[customer_id] = public_key
        self._audit.append("DEVICE_REGISTERED", {"customer_id": customer_id}, at=self._clock())

    def issue_challenge(self, plan: ResolvedPlan) -> Challenge:
        now = self._clock()
        challenge = Challenge(
            nonce=secrets.token_hex(16),
            plan_hash=plan_hash(plan),
            customer_id=plan.customer_id,
            expires_at=now + self._ttl,
        )
        self._pending[challenge.nonce] = challenge
        self._audit.append(
            "CHALLENGE_ISSUED",
            {"nonce": challenge.nonce, "plan_hash": challenge.plan_hash, "plan": plan.model_dump(mode="json")},
            at=now,
        )
        return challenge

    def execute(
        self, plan: ResolvedPlan, *, nonce: str | None = None, signature: bytes | None = None
    ) -> list[Receipt]:
        try:
            return self._execute(plan, nonce, signature)
        except GatewayError as e:
            self._audit.append(
                "REJECTED",
                {"code": e.code, "nonce": nonce, "plan_hash": plan_hash(plan)},
                at=self._clock(),
            )
            raise

    def _execute(self, plan: ResolvedPlan, nonce: str | None, signature: bytes | None) -> list[Receipt]:
        now = self._clock()
        if not nonce or not signature:
            raise GatewayError("SIGNATURE_REQUIRED", "no customer approval attached")
        if nonce in self._consumed:
            raise GatewayError("NONCE_CONSUMED", "this approval was already used")
        challenge = self._pending.get(nonce)
        if challenge is None:
            raise GatewayError("UNKNOWN_CHALLENGE", "no such approval request")
        if now > challenge.expires_at:
            del self._pending[nonce]
            raise GatewayError("CHALLENGE_EXPIRED", "approval request expired")
        public_key = self._device_keys.get(challenge.customer_id)
        if public_key is None or not verify_signature(public_key, signature, challenge_digest(challenge)):
            raise GatewayError("SIGNATURE_INVALID", "signature is not from the customer's device")
        if plan_hash(plan) != challenge.plan_hash:
            raise GatewayError("PAYLOAD_HASH_MISMATCH", "plan differs from what the customer approved")

        # The approval is genuine and matches this plan: it is now used up,
        # whether or not execution below succeeds.
        del self._pending[nonce]
        self._consumed.add(nonce)

        new_balances = self._simulate(plan, now)
        for account_id, balance in new_balances.items():
            self._bank.accounts[account_id].balance = balance

        receipts = [
            Receipt(f"GV{next(self._receipt_ids):08d}", s.id, f"S${s.amount:,.2f} to {s.to_display}")
            for s in plan.steps
        ]
        self._audit.append(
            "EXECUTED",
            {
                "nonce": nonce,
                "plan_hash": challenge.plan_hash,
                "signature": signature.hex(),
                "receipts": [r.__dict__ for r in receipts],
            },
            at=now,
        )
        return receipts

    def _simulate(self, plan: ResolvedPlan, now: datetime) -> dict[str, Decimal]:
        """Re-check the plan against live state; returns balances if all steps succeed."""
        bank = self._bank
        if plan.customer_id != bank.customer_id:
            raise GatewayError("STALE_PLAN", "plan belongs to a different customer")
        balances = {a.id: a.balance for a in bank.accounts.values()}
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
                        or quote.price != fill.price
                        or quote.expires_at != fill.quote_expires_at
                        or fill.price * fill.quantity != s.amount
                    ):
                        raise GatewayError("STALE_PLAN", "equity fill does not match an issued quote")
                    if now > quote.expires_at:
                        raise GatewayError("QUOTE_EXPIRED", f"quote {quote.quote_id} expired")
            if s.amount <= 0:
                raise GatewayError("STALE_PLAN", f"non-positive amount in {s.id}")
            if balances[s.from_account] < s.amount:
                raise GatewayError("INSUFFICIENT_FUNDS", f"{s.from_account} cannot cover {s.id}")
            balances[s.from_account] -= s.amount
            if s.type == "own_transfer":
                balances[s.to] += s.amount
        if balances != plan.balances_after:
            raise GatewayError("STALE_PLAN", "balances changed since the plan was approved")
        return balances
