"""Deterministic risk rules: what a resolved plan needs before it may be signed.

The LLM is never consulted. The rules read only the ResolvedPlan (bank ids and
Decimal amounts from the evaluator), the bank's own view of the customer, and
the gateway's execution history, so the same inputs always give the same
decision and anyone holding them (the gateway, an auditor) can recompute it.

Choices worth knowing:

- Outcome precedence is deny > hold > step_up > allow, but every rule that
  fires is reported, so the audit shows all the reasons, not just the worst.
- Own-account transfers never leave the customer, so no third-party limit
  applies to them. Share buys are not "payments to others" (no per-transaction
  or daily cap) but cash does leave, so they count toward step-up.
- Rules that compare a total (daily limit, new payee, outlier, step-up) add up
  the whole plan, so three S$3,000 steps cannot dodge a check that one S$9,000
  step would trip. The reason names the step where the threshold is crossed.
- Fail closed: an unknown KYC tier gets the tightest limits, a step naming
  anything the bank view cannot vouch for is denied, and so is a plan that
  skipped schema validation (``model_construct``/``model_copy``), since one
  negative amount could cancel out another step in every total below.
- The bank day runs on Singapore time. ``Payee.added_on`` is only a date, so
  the 12-hour cooling-off assumes the payee was added at the last moment of
  that day.
- Reason messages are fixed templates filled only with amounts, limits and
  bank-record names. Nothing from a plan is echoed unless the bank knows it.
- The decision id binds a decision to one exact plan. It is a plain hash, not
  a MAC: it proves which plan a decision was for, and the gateway can re-run
  these rules and compare, but it does not prove who computed it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from .bank import BankView, Payee
from .crypto import canonical_bytes, plan_hash, sha256_hex
from pydantic import ValidationError

from .schema import PolicyDecision, PolicyReason, ResolvedPlan, ResolvedStep

if TYPE_CHECKING:  # the control plane only reads executions; it never imports the gateway at runtime
    from .gateway import Execution

Outcome = Literal["allow", "step_up", "hold", "deny"]

# --- Limits (mock values, plausible for a Singapore retail bank) ------------


@dataclass(frozen=True)
class TierLimits:
    per_txn: Decimal  # largest single payment to others
    daily: Decimal  # payments to others per bank day (Singapore time)


TIER_LIMITS: Mapping[str, TierLimits] = MappingProxyType({
    # Newly onboarded or simplified-KYC customers.
    "basic": TierLimits(per_txn=Decimal("1000.00"), daily=Decimal("2000.00")),
    # Default for a verified retail customer.
    "standard": TierLimits(per_txn=Decimal("10000.00"), daily=Decimal("20000.00")),
    # Wealth-segment customers who have raised their limits.
    "premium": TierLimits(per_txn=Decimal("50000.00"), daily=Decimal("100000.00")),
})
FALLBACK_TIER = "basic"  # an unknown tier gets the tightest limits

KYC_VERIFIED = "verified"

# Cash leaving the customer's accounts in one request at or above this needs step-up auth.
STEP_UP_THRESHOLD = Decimal("5000.00")

# A payee is "new" for its first 7 days on file, and until it has a payment on
# record from before the last 7 days. While new, at most S$1,000 in total may
# go to it within 7 days without a cooling-off hold.
NEW_PAYEE_DAYS = 7
NEW_PAYEE_LIMIT = Decimal("1000.00")

# Any payment to a payee added less than 12 hours ago is held (cooling-off).
NEW_PAYEE_COOLING = timedelta(hours=12)

# More than 10x the largest past payment to a payee (or 10x a bill's amount due) needs step-up.
OUTLIER_MULTIPLE = 10

# 5 or more payments to others within 10 minutes (this plan included) are held.
VELOCITY_MAX_STEPS = 5
VELOCITY_WINDOW = timedelta(minutes=10)

# Fixed UTC+8 offset: Singapore has no daylight saving, and this needs no tz database.
BANK_TZ = timezone(timedelta(hours=8), "SGT")

THIRD_PARTY_TYPES = frozenset({"paynow", "bill_payment"})
OUTGOING_TYPES = THIRD_PARTY_TYPES | {"equity_buy"}

OUTCOME_SEVERITY: Mapping[str, int] = MappingProxyType({"allow": 0, "step_up": 1, "hold": 2, "deny": 3})

RULE_OUTCOMES: Mapping[str, Outcome] = MappingProxyType({
    "MALFORMED_PLAN": "deny",
    "CUSTOMER_MISMATCH": "deny",
    "UNKNOWN_COUNTERPARTY": "deny",
    "KYC_NOT_VERIFIED": "deny",
    "INVESTMENT_NOT_ELIGIBLE": "deny",
    "PER_TXN_LIMIT": "deny",
    "DAILY_LIMIT": "deny",
    "NEW_PAYEE_COOLING": "hold",
    "NEW_PAYEE_LIMIT": "hold",
    "VELOCITY": "hold",
    "AMOUNT_OUTLIER": "step_up",
    "STEP_UP_AMOUNT": "step_up",
})


def tier_limits(tier: str) -> TierLimits:
    return TIER_LIMITS.get(tier, TIER_LIMITS[FALLBACK_TIER])


def decision_id(plan: ResolvedPlan, outcome: str, reasons: Sequence[PolicyReason]) -> str:
    return sha256_hex(canonical_bytes({
        "plan_hash": plan_hash(plan),
        "outcome": outcome,
        "reasons": [r.model_dump(mode="json") for r in reasons],
    }))


def evaluate_policy(
    plan: ResolvedPlan,
    view: BankView,
    *,
    now: datetime,
    history: Sequence[Execution],
) -> PolicyDecision:
    """`history` must reach back at least NEW_PAYEE_DAYS for the new-payee total to be complete."""
    if well_formed(plan):
        facts = _Facts.gather(plan, view, now, history)
        reasons = tuple(reason for rule in _RULES for reason in rule(facts))
    else:
        reasons = (_reason("MALFORMED_PLAN", None, "this request is malformed and cannot be checked"),)
    outcome = outcome_for(reasons)
    return PolicyDecision(id=decision_id(plan, outcome, reasons), outcome=outcome, reasons=reasons)


def well_formed(plan: ResolvedPlan) -> bool:
    """True if the plan passes the schema's own checks (positive whole-cent amounts, fills, ids)."""
    try:
        ResolvedPlan.model_validate(plan.model_dump(warnings=False))
    except (ValidationError, TypeError, ValueError):
        return False
    return True


def outcome_for(reasons: Sequence[PolicyReason]) -> Outcome:
    """The most severe outcome among the reasons (deny > hold > step_up > allow)."""
    return max(
        (RULE_OUTCOMES[r.code] for r in reasons), key=OUTCOME_SEVERITY.__getitem__, default="allow"
    )


# --- Facts the rules read ----------------------------------------------------


@dataclass(frozen=True)
class _Facts:
    plan: ResolvedPlan
    view: BankView
    now: datetime  # timezone-aware
    today: date  # bank day in Singapore
    history: tuple[tuple[datetime, ResolvedStep], ...]  # (timezone-aware time, executed step)
    limits: TierLimits

    @classmethod
    def gather(
        cls, plan: ResolvedPlan, view: BankView, now: datetime, history: Sequence[Execution]
    ) -> _Facts:
        now = _aware(now)
        return cls(
            plan=plan,
            view=view,
            now=now,
            today=now.astimezone(BANK_TZ).date(),
            history=tuple((_aware(e.at), e.step) for e in history),
            limits=tier_limits(view.kyc_tier),
        )

    def steps(self, types: frozenset[str]) -> list[ResolvedStep]:
        return [s for s in self.plan.steps if s.type in types]

    def paid_to(self, payee_id: str) -> list[tuple[datetime, Decimal]]:
        return [(at, s.amount) for at, s in self.history if s.type == "paynow" and s.to == payee_id]


def _aware(at: datetime) -> datetime:
    """Naive datetimes are taken as UTC (the gateway's clock) so comparisons never raise."""
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


# --- Rules (each yields its reasons in step order) ---------------------------


def _customer_mismatch(f: _Facts) -> Iterator[PolicyReason]:
    if f.plan.customer_id != f.view.customer_id:
        yield _reason("CUSTOMER_MISMATCH", None, "this request was prepared for a different customer")


def _unknown_counterparty(f: _Facts) -> Iterator[PolicyReason]:
    for s in f.plan.steps:
        if not _on_profile(s, f.view):
            yield _reason(
                "UNKNOWN_COUNTERPARTY", s.id,
                "this request names an account, payee, biller or share that is not on your profile",
            )


def _kyc_not_verified(f: _Facts) -> Iterator[PolicyReason]:
    if f.view.kyc_status != KYC_VERIFIED and f.steps(OUTGOING_TYPES):
        yield _reason(
            "KYC_NOT_VERIFIED", None,
            "payments to others and share purchases are blocked until your identity "
            "verification (KYC) is complete",
        )


def _investment_not_eligible(f: _Facts) -> Iterator[PolicyReason]:
    if not f.view.investment_eligible and f.steps(frozenset({"equity_buy"})):
        yield _reason(
            "INVESTMENT_NOT_ELIGIBLE", None,
            "share purchases need the investment suitability check (CKA), which your profile has not passed",
        )


def _per_txn_limit(f: _Facts) -> Iterator[PolicyReason]:
    for s in f.steps(THIRD_PARTY_TYPES):
        if s.amount > f.limits.per_txn:
            yield _reason(
                "PER_TXN_LIMIT", s.id,
                f"{_sgd(s.amount)} to {_name(s, f.view)} is above your "
                f"{_sgd(f.limits.per_txn)} per-transaction limit",
            )


def _daily_limit(f: _Facts) -> Iterator[PolicyReason]:
    spent_today = sum(
        (s.amount for at, s in f.history
         if s.type in THIRD_PARTY_TYPES and at.astimezone(BANK_TZ).date() == f.today),
        Decimal(0),
    )
    planned = f.steps(THIRD_PARTY_TYPES)
    crossing = _first_crossing(planned, spent_today, lambda total: total > f.limits.daily)
    if crossing is not None:
        total = spent_today + sum(s.amount for s in planned)
        yield _reason(
            "DAILY_LIMIT", crossing.id,
            f"this would take today's payments to others to {_sgd(total)}, above your "
            f"{_sgd(f.limits.daily)} daily limit",
        )


def _new_payee_cooling(f: _Facts) -> Iterator[PolicyReason]:
    for payee, steps in _by_payee(f):
        if f.now < _cooling_ends(payee.added_on):
            yield _reason(
                "NEW_PAYEE_COOLING", steps[0].id,
                f"{payee.legal_name} was added as a payee less than {_hours(NEW_PAYEE_COOLING)} hours ago, "
                "so payments to them are on hold",
            )


def _new_payee_limit(f: _Facts) -> Iterator[PolicyReason]:
    for payee, steps in _by_payee(f):
        if not _is_new(payee, f):
            continue
        recent = _recently_paid(payee, f)
        crossing = _first_crossing(steps, recent, lambda total: total > NEW_PAYEE_LIMIT)
        if crossing is not None:
            total = recent + sum(s.amount for s in steps)
            yield _reason(
                "NEW_PAYEE_LIMIT", crossing.id,
                f"payments to {payee.legal_name}, a new payee, would reach {_sgd(total)} "
                f"within {NEW_PAYEE_DAYS} days, above the {_sgd(NEW_PAYEE_LIMIT)} new-payee limit",
            )


def _velocity(f: _Facts) -> Iterator[PolicyReason]:
    since = f.now - VELOCITY_WINDOW
    recent = sum(1 for at, s in f.history if s.type in THIRD_PARTY_TYPES and at >= since)
    planned = f.steps(THIRD_PARTY_TYPES)
    total = recent + len(planned)
    if planned and total >= VELOCITY_MAX_STEPS:
        crossing = planned[max(0, VELOCITY_MAX_STEPS - recent - 1)]
        yield _reason(
            "VELOCITY", crossing.id,
            f"this would make {total} payments to others within "
            f"{_minutes(VELOCITY_WINDOW)} minutes, so it is paused for review",
        )


def _amount_outlier(f: _Facts) -> Iterator[PolicyReason]:
    for key, steps in _group(f.plan.steps, THIRD_PARTY_TYPES).items():
        kind, target = key
        if kind == "paynow":
            payee = f.view.payees.get(target)
            if payee is None:
                continue
            past = [p.amount for p in payee.history] + [amount for _, amount in f.paid_to(target)]
            if not past:
                continue  # never paid: the new-payee rules cover it
            baseline, name = max(past), payee.legal_name
            what = f"the largest past payment to them ({_sgd(baseline)})"
        else:
            biller = f.view.billers.get(target)
            if biller is None or biller.amount_due <= 0:
                continue
            baseline, name = biller.amount_due, biller.name
            what = f"the amount due ({_sgd(baseline)})"
        ceiling = baseline * OUTLIER_MULTIPLE
        crossing = _first_crossing(steps, Decimal(0), lambda total: total > ceiling)
        if crossing is not None:
            total = sum(s.amount for s in steps)
            yield _reason(
                "AMOUNT_OUTLIER", crossing.id,
                f"{_sgd(total)} to {name} is more than {OUTLIER_MULTIPLE} times {what}",
            )


def _step_up_amount(f: _Facts) -> Iterator[PolicyReason]:
    outgoing = f.steps(OUTGOING_TYPES)
    crossing = _first_crossing(outgoing, Decimal(0), lambda total: total >= STEP_UP_THRESHOLD)
    if crossing is not None:
        total = sum(s.amount for s in outgoing)
        yield _reason(
            "STEP_UP_AMOUNT", crossing.id,
            f"{_sgd(total)} would leave your accounts, and {_sgd(STEP_UP_THRESHOLD)} or more "
            "needs extra verification",
        )


# Order matters only for how reasons are listed: most severe rules first.
_RULES: tuple[Callable[[_Facts], Iterator[PolicyReason]], ...] = (
    _customer_mismatch,
    _unknown_counterparty,
    _kyc_not_verified,
    _investment_not_eligible,
    _per_txn_limit,
    _daily_limit,
    _new_payee_cooling,
    _new_payee_limit,
    _velocity,
    _amount_outlier,
    _step_up_amount,
)


# --- Helpers -----------------------------------------------------------------


def _reason(code: str, step_id: str | None, message: str) -> PolicyReason:
    return PolicyReason(code=code, step_id=step_id, message=message)


def _on_profile(s: ResolvedStep, view: BankView) -> bool:
    if s.from_account not in view.accounts:
        return False
    match s.type:
        case "own_transfer":
            return s.to in view.accounts and s.to != s.from_account
        case "paynow":
            return s.to in view.payees
        case "bill_payment":
            return s.to in view.billers
        case "equity_buy":
            return view.market.name(s.to) is not None
    return False  # pragma: no cover - StepType is exhaustive


def _name(s: ResolvedStep, view: BankView) -> str:
    """A counterparty's name from the bank's records; never text carried by the plan."""
    if s.type == "paynow" and s.to in view.payees:
        return view.payees[s.to].legal_name
    if s.type == "bill_payment" and s.to in view.billers:
        return view.billers[s.to].name
    return "an unknown recipient"


def _group(
    steps: Sequence[ResolvedStep], types: frozenset[str]
) -> dict[tuple[str, str], list[ResolvedStep]]:
    """Steps by (type, counterparty), in order of first appearance."""
    groups: dict[tuple[str, str], list[ResolvedStep]] = {}
    for s in steps:
        if s.type in types:
            groups.setdefault((s.type, s.to), []).append(s)
    return groups


def _by_payee(f: _Facts) -> Iterator[tuple[Payee, list[ResolvedStep]]]:
    for (_, payee_id), steps in _group(f.plan.steps, frozenset({"paynow"})).items():
        payee = f.view.payees.get(payee_id)
        if payee is not None:
            yield payee, steps


def _first_crossing(
    steps: Sequence[ResolvedStep], start: Decimal, crosses: Callable[[Decimal], bool]
) -> ResolvedStep | None:
    """The step at which a running total (from `start`) first crosses a threshold."""
    running = start
    for s in steps:
        running += s.amount
        if crosses(running):
            return s
    return None


def _cooling_ends(added_on: date) -> datetime:
    # Only the date is known, so assume the payee was added just before midnight (Singapore time).
    return datetime.combine(added_on + timedelta(days=1), time(), tzinfo=BANK_TZ) + NEW_PAYEE_COOLING


def _is_new(payee: Payee, f: _Facts) -> bool:
    if (f.today - payee.added_on).days < NEW_PAYEE_DAYS:
        return True
    window_start = f.now - timedelta(days=NEW_PAYEE_DAYS)
    paid_before_window = any((f.today - p.on).days >= NEW_PAYEE_DAYS for p in payee.history) or any(
        at <= window_start for at, _ in f.paid_to(payee.id)
    )
    return not paid_before_window


def _recently_paid(payee: Payee, f: _Facts) -> Decimal:
    """Paid to this payee within the new-payee window (bank records plus this gateway's executions)."""
    window_start = f.now - timedelta(days=NEW_PAYEE_DAYS)
    on_record = sum((p.amount for p in payee.history if (f.today - p.on).days < NEW_PAYEE_DAYS), Decimal(0))
    executed = sum((amount for at, amount in f.paid_to(payee.id) if at > window_start), Decimal(0))
    return on_record + executed


def _sgd(value: Decimal) -> str:
    return f"S${value:,.2f}"


def _hours(span: timedelta) -> int:
    return int(span.total_seconds() // 3600)


def _minutes(span: timedelta) -> int:
    return int(span.total_seconds() // 60)
