"""Confirmation renderer: the exact words the customer sees and hears before signing.

Everything here comes from fixed templates filled with ResolvedPlan fields and
the bank's own records, so no LLM text can reach the customer at the moment
they approve:

- Names are looked up in the bank view by id. ``ResolvedStep.to_display`` is
  ignored, because a forged plan could carry any display text next to a
  different payee id.
- Biller references and payee nicknames are untrusted free text and are never
  used. Payees are shown by legal name and masked PayNow proxy.
- Policy lines come from this module's templates, keyed by reason code, not
  from ``PolicyReason.message``. The decision must hash to this exact plan, so
  a decision made for a milder plan cannot be passed off on this one.

``spoken_script`` is plain English for text-to-speech: no digits, symbols or
abbreviations. Its sha256 (``script_hash``) goes into the signed challenge, so
the signature covers the words the customer heard. Amounts of S$1,000 or more,
and amounts policy flags as unusual, are read twice, in words and then digit
by digit, because "fifteen"/"fifty" and "fifty"/"fifty thousand" are the
mishearings that cost the most.
"""

from __future__ import annotations

import re
from decimal import Decimal

from .bank import BankView, share_cost
from .crypto import sha256_hex
from .policy import (
    NEW_PAYEE_COOLING,
    NEW_PAYEE_DAYS,
    NEW_PAYEE_LIMIT,
    OUTLIER_MULTIPLE,
    RULE_OUTCOMES,
    STEP_UP_THRESHOLD,
    VELOCITY_MAX_STEPS,
    VELOCITY_WINDOW,
    decision_id,
    outcome_for,
    tier_limits,
    well_formed,
)
from .schema import ConfirmationView, PolicyDecision, PolicyReason, ResolvedPlan, ResolvedStep

CENT = Decimal("0.01")

# Amounts from this value up are also read digit by digit.
DIGIT_READBACK_FROM = Decimal("1000.00")

# spell_amount covers anything below a quadrillion dollars (schema Money tops out near ten billion).
_SPELL_LIMIT = Decimal(10) ** 15


class RenderError(Exception):
    """The plan cannot be shown truthfully from bank records, so it must not be signed."""

    def __init__(self, code: str, message: str, step_id: str | None = None):
        super().__init__(f"{code}: {message}" + (f" (step {step_id})" if step_id else ""))
        self.code = code
        self.message = message
        self.step_id = step_id


def render(plan: ResolvedPlan, policy: PolicyDecision, view: BankView) -> ConfirmationView:
    """The confirmation for one plan: screen lines, the spoken script and its hash.

    Raises RenderError rather than show anything bank records cannot vouch for,
    or a policy decision that was made for a different plan.
    """
    if not well_formed(plan):
        raise RenderError("INVALID_PLAN", "plan does not pass the schema's checks")
    if plan.customer_id != view.customer_id:
        raise RenderError("CUSTOMER_MISMATCH", "plan is for a different customer")
    if policy.id != decision_id(plan, policy.outcome, policy.reasons):
        raise RenderError("POLICY_MISMATCH", "policy decision was not made for this plan")
    unknown = [r.code for r in policy.reasons if r.code not in RULE_OUTCOMES]
    if unknown:
        raise RenderError("POLICY_MISMATCH", f"unknown policy reason {unknown[0]!r}")
    if policy.outcome != outcome_for(policy.reasons):
        raise RenderError("POLICY_MISMATCH", "policy outcome does not follow from its reasons")

    unusual = {r.step_id for r in policy.reasons if r.code == "AMOUNT_OUTLIER"}
    described = [_describe(s, view, unusual=s.id in unusual) for s in plan.steps]
    position = {s.id: n for n, s in enumerate(plan.steps, 1)}
    notes = [_policy_note(r, position, view) for r in policy.reasons]

    lines = tuple(written for written, _ in described) + tuple(dict.fromkeys(w for w, _ in notes))
    script = " ".join([
        f"Bank confirmation. Please check {_count(len(plan.steps), 'transaction')} before you approve.",
        *(f"Transaction {_int_words(n)}: {spoken}." for n, (_, spoken) in enumerate(described, 1)),
        *dict.fromkeys(spoken for _, spoken in notes),
        _CLOSING[policy.outcome],
    ])
    return ConfirmationView(
        lines=lines,
        spoken_script=script,
        script_hash=sha256_hex(script.encode("utf-8")),
        requires_step_up=_requires_step_up(policy),
    )


# --- Steps -------------------------------------------------------------------


def _describe(s: ResolvedStep, view: BankView, *, unusual: bool) -> tuple[str, str]:
    """(screen line, spoken clause) for one step, from bank records only."""
    source = _account(s.from_account, view, s.id)
    # "fifty dollars" or "one thousand dollars, that is one-zero-zero-zero dollars,"
    read_twice = unusual or s.amount >= DIGIT_READBACK_FROM
    said = spell_amount(s.amount) + (f", that is {read_digits(s.amount)}," if read_twice else "")

    match s.type:
        case "own_transfer":
            if s.to == s.from_account:
                raise RenderError("INCONSISTENT_STEP", "transfer to the same account", s.id)
            target = _account(s.to, view, s.id)
            return (
                f"Transfer {_sgd(s.amount)} from {source} to {target}",
                f"transfer {said} from your {source} to your {target}",
            )

        case "paynow":
            payee = view.payees.get(s.to)
            if payee is None:
                raise RenderError("UNKNOWN_COUNTERPARTY", "payee is not on the customer's profile", s.id)
            tail = re.search(r"([0-9]+)\s*$", payee.paynow_proxy)
            proxy = f", PayNow number ending {_digit_words(tail.group(1))}" if tail else ""
            return (
                f"PayNow {_sgd(s.amount)} to {payee.legal_name} ({payee.paynow_proxy}) from {source}",
                f"send {said} by PayNow to {payee.legal_name}{proxy}, from your {source}",
            )

        case "bill_payment":
            biller = view.billers.get(s.to)
            if biller is None:
                raise RenderError("UNKNOWN_COUNTERPARTY", "biller is not on the customer's profile", s.id)
            # biller.reference is untrusted free text and is deliberately never rendered.
            return (
                f"Pay {_sgd(s.amount)} to {biller.name} from {source}",
                f"pay {said} to {biller.name} from your {source}",
            )

        case "equity_buy":
            fill = s.equity
            name = view.market.name(s.to)
            if name is None:
                raise RenderError("UNKNOWN_COUNTERPARTY", "share is not listed", s.id)
            if fill is None or share_cost(fill.price, fill.quantity) != s.amount:
                raise RenderError("INCONSISTENT_STEP", "share quantity and price do not add up", s.id)
            shares = "share" if fill.quantity == 1 else "shares"
            return (
                f"Buy {fill.quantity} {name} ({fill.symbol}) {shares} at {_sgd_price(fill.price)} "
                f"= {_sgd(s.amount)} from {source}",
                f"buy {_count(fill.quantity, 'share')} of {name}, stock code {_code_words(fill.symbol)}, "
                f"at {_spell_price(fill.price)} per share, from your {source}, "
                f"for a total of {said.rstrip(',')}",
            )

    raise RenderError("UNKNOWN_STEP", f"cannot render step type {s.type!r}", s.id)  # pragma: no cover


def _account(account_id: str, view: BankView, step_id: str) -> str:
    """An account's name from bank records; an account the customer does not hold is a RenderError."""
    account = view.accounts.get(account_id)
    if account is None:
        raise RenderError("UNKNOWN_COUNTERPARTY", "account is not on the customer's profile", step_id)
    return account.name


# --- Policy notes ------------------------------------------------------------

_LABEL = {"step_up": "Extra verification needed", "hold": "On hold", "deny": "Blocked"}
_VERB = {"step_up": "needs extra verification", "hold": "will be held", "deny": "cannot go ahead"}
_CLOSING = {
    "allow": "If this is correct, approve with your passkey. If anything is wrong, do not approve.",
    "step_up": (
        "If this is correct, approve with your passkey, then complete the extra verification. "
        "If anything is wrong, do not approve."
    ),
    "hold": "If you approve, this request will be held, and no money will move until the hold ends.",
    "deny": "This request cannot be completed, and no money will move.",
}


def _policy_note(reason: PolicyReason, position: dict[str, int], view: BankView) -> tuple[str, str]:
    """(screen line, spoken sentence) for one reason, from this module's templates only."""
    outcome = RULE_OUTCOMES[reason.code]
    written, spoken = _reason_text(reason.code, view)
    n = position.get(reason.step_id) if reason.step_id else None
    where = f" (step {n})" if n else ""
    subject = f"Transaction {_int_words(n)}" if n else "This request"
    return (
        f"{_LABEL[outcome]}{where}: {written}",
        f"{subject} {_VERB[outcome]}, because {spoken}.",
    )


def _reason_text(code: str, view: BankView) -> tuple[str, str]:
    """The fixed wording for one reason code, as (screen line, spoken clause), with this customer's limits."""
    limits = tier_limits(view.kyc_tier)
    hours = int(NEW_PAYEE_COOLING.total_seconds() // 3600)
    minutes = int(VELOCITY_WINDOW.total_seconds() // 60)
    match code:
        case "MALFORMED_PLAN":
            return ("This request is malformed and cannot be checked.",
                    "it is malformed and cannot be checked")
        case "CUSTOMER_MISMATCH":
            return ("This request was prepared for a different customer.",
                    "it was prepared for a different customer")
        case "UNKNOWN_COUNTERPARTY":
            return ("It names something that is not on your profile.",
                    "it names something that is not on your profile")
        case "KYC_NOT_VERIFIED":
            return ("Payments to others and share purchases need a completed identity verification (KYC).",
                    "your identity verification is not yet complete")
        case "INVESTMENT_NOT_ELIGIBLE":
            return ("Share purchases need a completed investment suitability check (CKA).",
                    "your investment suitability check is not yet complete")
        case "PER_TXN_LIMIT":
            return (f"Above your {_sgd(limits.per_txn)} per-transaction limit.",
                    f"it is above your per-transaction limit of {spell_amount(limits.per_txn)}")
        case "DAILY_LIMIT":
            return (f"Takes today's payments to others above your {_sgd(limits.daily)} daily limit.",
                    f"it would take today's payments to others above your daily limit of "
                    f"{spell_amount(limits.daily)}")
        case "NEW_PAYEE_COOLING":
            return (f"The payee was added less than {hours} hours ago.",
                    f"the payee was added less than {_int_words(hours)} hours ago")
        case "NEW_PAYEE_LIMIT":
            return (f"More than {_sgd(NEW_PAYEE_LIMIT)} to a new payee within {NEW_PAYEE_DAYS} days.",
                    f"a new payee can receive at most {spell_amount(NEW_PAYEE_LIMIT)} "
                    f"in {_int_words(NEW_PAYEE_DAYS)} days")
        case "VELOCITY":
            return (f"{VELOCITY_MAX_STEPS} or more payments to others within {minutes} minutes.",
                    f"there would be {_int_words(VELOCITY_MAX_STEPS)} or more payments to others "
                    f"within {_int_words(minutes)} minutes")
        case "AMOUNT_OUTLIER":
            return (f"More than {OUTLIER_MULTIPLE} times the usual amount for this recipient.",
                    f"it is more than {_int_words(OUTLIER_MULTIPLE)} times the usual amount for this recipient")
        case "STEP_UP_AMOUNT":
            return (f"{_sgd(STEP_UP_THRESHOLD)} or more is leaving your accounts.",
                    f"{spell_amount(STEP_UP_THRESHOLD)} or more is leaving your accounts")
    raise RenderError("POLICY_MISMATCH", f"no template for policy reason {code!r}")  # pragma: no cover


def _requires_step_up(policy: PolicyDecision) -> bool:
    # A held plan that also tripped a step-up rule still needs it when the hold ends.
    if policy.outcome == "step_up":
        return True
    return policy.outcome == "hold" and any(RULE_OUTCOMES.get(r.code) == "step_up" for r in policy.reasons)


# --- Numbers in words --------------------------------------------------------

_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen"
).split()
_DIGITS = "0123456789"
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_SCALES = ((10**12, "trillion"), (10**9, "billion"), (10**6, "million"), (10**3, "thousand"))


def spell_amount(value: Decimal) -> str:
    """British English: 1000.5 -> "one thousand dollars and fifty cents"; 101 -> "one hundred and one dollars"."""
    cents_total = _cents(value)
    dollars, cents = divmod(cents_total, 100)
    parts = []
    if dollars:
        parts.append(_count(dollars, "dollar"))
    if cents:
        parts.append(_count(cents, "cent"))
    return " and ".join(parts) or "zero dollars"


def read_digits(value: Decimal) -> str:
    """Digit by digit: 50 -> "five-zero dollars"; 1000.5 -> "one-zero-zero-zero dollars and five-zero cents"."""
    dollars, cents = divmod(_cents(value), 100)
    said = f"{_digit_words(str(dollars))} {'dollar' if dollars == 1 else 'dollars'}"
    if cents:
        said += f" and {_digit_words(f'{cents:02d}')} {'cent' if cents == 1 else 'cents'}"
    return said


def _cents(value: Decimal) -> int:
    # Floats are refused: 0.1 + 0.2 is not a sum of money.
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise TypeError(f"amount must be a Decimal, not {type(value).__name__}")
    value = Decimal(value)
    if not value.is_finite() or value < 0 or value >= _SPELL_LIMIT:
        raise ValueError(f"cannot spell amount {value}")
    if value != value.quantize(CENT):
        raise ValueError(f"amount {value} has fractions of a cent")
    return int(value * 100)


def _int_words(n: int) -> str:
    """A whole number in British English words: 1005 -> "one thousand and five"."""
    if n < 0:
        raise ValueError("negative")
    if n == 0:
        return "zero"
    parts = []
    for scale, name in _SCALES:
        count, n = divmod(n, scale)
        if count:
            parts.append(f"{_int_words(count)} {name}")
    if n:
        # British usage: "one thousand and five", but "one thousand one hundred".
        parts.append(f"and {_below_hundred(n)}" if parts and n < 100 else _below_thousand(n))
    return " ".join(parts)


def _below_thousand(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = [f"{_ONES[hundreds]} hundred"] if hundreds else []
    if rest:
        parts.append(_below_hundred(rest))
    return " and ".join(parts)


def _below_hundred(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")


def _count(n: int, noun: str) -> str:
    return f"{_int_words(n)} {noun}{'' if n == 1 else 's'}"


def _digit_words(digits: str) -> str:
    """Digits read one by one: "4521" -> "four-five-two-one"."""
    return "-".join(_ONES[int(d)] for d in digits)


def _code_words(symbol: str) -> str:
    """Stock codes character by character: "D05" -> "D zero five"."""
    return " ".join(_ONES[int(c)] if c in _DIGITS else c.upper() for c in symbol if c.isalnum())


def _spell_price(price: Decimal) -> str:
    if price == price.quantize(CENT):
        return spell_amount(price)
    # SGX ticks can go below a cent (e.g. S$0.905): read it as a decimal.
    whole, _, fraction = f"{price.normalize():f}".partition(".")
    return f"{_int_words(int(whole))} point {_digit_words(fraction)} dollars"


def _sgd(value: Decimal) -> str:
    return f"S${value:,.2f}"


def _sgd_price(price: Decimal) -> str:
    return _sgd(price) if price == price.quantize(CENT) else f"S${price.normalize():,f}"
