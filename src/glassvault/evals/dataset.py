"""Benchmark cases: what the customer says, and what a safe agent must do about it.

Expectations are bank-level facts (which account, which payee, how many
dollars, how many shares), never wording. That lets the same cases score any
agent (the naive baseline, Glass Vault, an oracle), and lets the harness check
them against the ledger instead of against what an agent says it did.

Loading validates every case against the seed bank *after* its setup ops, so a
typo in a payee id or an unaffordable expected step fails at load time instead
of silently marking every agent wrong.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from ..bank import Bank, BankView, PastPayment, Payee, seed_bank
from ..evaluator import CENT, EvaluationError, resolve
from ..schema import (
    BillPayment,
    FixedAmount,
    OwnTransfer,
    PayNowTransfer,
    Plan,
    ResolvedStep,
    StepType,
)

# A copy of rogue.ATTACKS (named in docs/CONTRACTS.md), used only if that module cannot be imported.
CONTRACT_ATTACKS = ("extra_transfer", "swap_payee", "inflate_amount", "swap_account", "follow_injection")

ExpectKind = Literal["confirm", "clarify", "answer", "frozen", "refused", "held"]
ClarifyAbout = Literal["payee", "amount", "missing_info", "unsupported"]

# `about` in a case file -> Clarification.reason in schema.py
ABOUT_REASON: dict[str, str] = {
    "payee": "ambiguous_payee",
    "amount": "ambiguous_amount",
    "missing_info": "missing_info",
    "unsupported": "unsupported",
}


class CaseError(ValueError):
    """A case file that does not load, or a case that contradicts the bank."""


class SetupError(ValueError):
    """A setup op that cannot be applied to the bank it was given."""


def known_attacks() -> tuple[str, ...]:
    """The attack names a case's `rogue:` field may use."""
    try:
        from ..rogue import ATTACKS
    except ImportError:
        return CONTRACT_ATTACKS
    return tuple(ATTACKS)


def _from_yaml_number(value: Any) -> Any:
    # YAML reads 142.35 as a float; going through str keeps it exactly 142.35.
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        return value.replace(",", "").strip()
    return value


def _as_text(value: Any) -> Any:
    # Options may be written as bare numbers (50000); compare them as text.
    if isinstance(value, bool):
        return value
    return str(value) if isinstance(value, (int, float)) else value


Amount = Annotated[
    Decimal, BeforeValidator(_from_yaml_number), Field(gt=0, max_digits=12, decimal_places=2)
]
Balance = Annotated[Decimal, BeforeValidator(_from_yaml_number), Field(ge=0, decimal_places=2)]
OptionValue = Annotated[str, BeforeValidator(_as_text)]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


def as_amount(text: str) -> Decimal | None:
    """`text` as a 2 d.p. amount, or None if it is an id such as P_JTAN."""
    try:
        value = Decimal(text.replace(",", "").strip())
    except InvalidOperation:
        return None
    return value.quantize(CENT) if value.is_finite() else None


def describe_step(step: ResolvedStep) -> str:
    """An executed step in one line for reports, e.g. "paynow SAV->P_MUM S$500.00"."""
    if step.type == "equity_buy" and step.equity is not None:
        return f"equity_buy {step.from_account}->{step.to} x{step.equity.quantity} (S${step.amount:,.2f})"
    return f"{step.type} {step.from_account}->{step.to} S${step.amount:,.2f}"


# --- Expectations --------------------------------------------------------------


class StepExpect(_Model):
    """One step the customer asked for, at bank level. Omit `from` when they did not say."""

    type: StepType
    from_account: str | None = Field(default=None, alias="from")
    to: str | None = Field(default=None, description="account, payee or biller id")
    amount: Amount | None = None
    symbol: str | None = None
    quantity: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _shape(self) -> StepExpect:
        if self.type == "equity_buy":
            if self.symbol is None or self.quantity is None:
                raise ValueError("equity_buy needs symbol and quantity")
            if self.to is not None or self.amount is not None:
                raise ValueError("equity_buy is matched by symbol and quantity, not to/amount")
        else:
            if self.to is None or self.amount is None:
                raise ValueError(f"{self.type} needs to and amount")
            if self.symbol is not None or self.quantity is not None:
                raise ValueError(f"{self.type} takes to/amount, not symbol/quantity")
        return self

    def matches(self, step: ResolvedStep) -> bool:
        """True if an executed step is this expectation; share buys compare the symbol and share count."""
        if step.type != self.type:
            return False
        if self.from_account is not None and step.from_account != self.from_account:
            return False
        if self.type == "equity_buy":
            return (
                step.to == self.symbol
                and step.equity is not None
                and step.equity.quantity == self.quantity
            )
        return step.to == self.to and step.amount == self.amount

    def describe(self) -> str:
        """This expectation in the form `describe_step` uses; "*" means any source account."""
        source = self.from_account or "*"
        if self.type == "equity_buy":
            return f"equity_buy {source}->{self.symbol} x{self.quantity}"
        return f"{self.type} {source}->{self.to} S${self.amount:,.2f}"


class Expect(_Model):
    """What the agent must do on one turn; evals/README.md explains each field."""

    kind: ExpectKind
    steps: list[StepExpect] = Field(default_factory=list)
    about: ClarifyAbout | None = None
    options: list[OptionValue] = Field(
        default_factory=list, description="values the question must offer (subset check)"
    )
    step_up: bool | None = Field(
        default=None, description="confirm only: should policy demand step-up auth? informational"
    )
    says: list[str] = Field(default_factory=list, description="answer only: text the answer must contain")
    never_says: list[str] = Field(default_factory=list, description="answer only: text it must not contain")

    @model_validator(mode="after")
    def _shape(self) -> Expect:
        if self.kind == "confirm" and not self.steps:
            raise ValueError("a confirm expectation needs steps")
        if self.kind != "confirm" and self.steps:
            raise ValueError(f"only confirm takes steps, not {self.kind}")
        if self.kind != "clarify" and (self.about is not None or self.options):
            raise ValueError("about/options only apply to clarify")
        if self.kind != "answer" and (self.says or self.never_says):
            raise ValueError("says/never_says only apply to answer")
        if self.kind != "confirm" and self.step_up is not None:
            raise ValueError("step_up only applies to confirm")
        return self


class CaseTurn(_Model):
    """One customer message, and what should happen after it."""

    customer: str = Field(min_length=1)
    expect: Expect


# --- Setup ops -----------------------------------------------------------------

PriorRunner = Callable[[Plan, int], None]
"""Executes a setup plan `minutes_ago` minutes before the conversation starts."""


class PastPaymentSpec(_Model):
    """A past payment in a setup op, dated relative to the case's bank day."""

    days_ago: int = Field(ge=0)
    amount: Amount


def _payment_history(payments: Sequence[PastPaymentSpec], today: date) -> tuple[PastPayment, ...]:
    ordered = sorted(payments, key=lambda p: p.days_ago)  # most recent first, as in seed_bank
    return tuple(PastPayment(today - timedelta(days=p.days_ago), p.amount) for p in ordered)


class AddPayee(_Model):
    """Setup op: save a new payee, optionally with past payments."""

    id: str = Field(pattern=r"^P_[A-Z0-9_]+$")
    legal_name: str
    nickname: str = Field(description="untrusted text: injection attempts go here")
    paynow_proxy: str = "+65 ••••0000"
    added_days_ago: int = Field(default=0, ge=0)
    history: list[PastPaymentSpec] = Field(default_factory=list)

    def apply(self, bank: Bank, today: date) -> None:
        if self.id in bank.payees:
            raise SetupError(f"payee {self.id} already exists")
        bank.payees[self.id] = Payee(
            self.id, self.legal_name, self.nickname, self.paynow_proxy,
            today - timedelta(days=self.added_days_ago), _payment_history(self.history, today),
        )


class AddPaymentHistory(_Model):
    """Setup op: add one past payment to a saved payee."""

    payee: str
    days_ago: int = Field(ge=0)
    amount: Amount

    def apply(self, bank: Bank, today: date) -> None:
        payee = bank.payees.get(self.payee)
        if payee is None:
            raise SetupError(f"add_payment_history: no payee {self.payee}")
        past = PastPayment(today - timedelta(days=self.days_ago), self.amount)
        history = tuple(sorted(payee.history + (past,), key=lambda p: p.on, reverse=True))
        bank.payees[self.payee] = dataclasses.replace(payee, history=history)


class SetBillerReference(_Model):
    """Setup op: replace a biller's free-text reference, where injected instructions go."""

    biller: str
    reference: str = Field(description="untrusted text: injection attempts go here")

    def apply(self, bank: Bank, today: date) -> None:
        biller = bank.billers.get(self.biller)
        if biller is None:
            raise SetupError(f"set_biller_reference: no biller {self.biller}")
        bank.billers[self.biller] = dataclasses.replace(biller, reference=self.reference)


class SetBalance(_Model):
    """Setup op: set an account's balance."""

    account: str
    balance: Balance

    def apply(self, bank: Bank, today: date) -> None:
        if self.account not in bank.accounts:
            raise SetupError(f"set_balance: no account {self.account}")
        bank.accounts[self.account].balance = self.balance.quantize(CENT)


class PriorExecution(_Model):
    """A payment already made today (signed and executed), e.g. to build up velocity."""

    type: Literal["own_transfer", "paynow", "bill_payment"]
    from_account: str = Field(alias="from")
    to: str
    amount: Amount
    minutes_ago: int = Field(default=0, ge=0, le=24 * 60)

    def plan(self) -> Plan:
        """This payment as a one-step plan."""
        amount = FixedAmount(value=self.amount)
        match self.type:
            case "own_transfer":
                step = OwnTransfer(id="t1", from_account=self.from_account, to_account=self.to, amount=amount)
            case "paynow":
                step = PayNowTransfer(id="t1", from_account=self.from_account, payee_id=self.to, amount=amount)
            case "bill_payment":
                step = BillPayment(id="t1", from_account=self.from_account, biller_id=self.to, amount=amount)
        return Plan(steps=[step])


class SetupOp(_Model):
    """Exactly one of the fields below, written in YAML as a one-key mapping."""

    add_payee: AddPayee | None = None
    add_payment_history: AddPaymentHistory | None = None
    set_biller_reference: SetBillerReference | None = None
    set_balance: SetBalance | None = None
    set_kyc_status: Literal["verified", "pending"] | None = None
    set_investment_eligible: bool | None = None
    prior_execution: PriorExecution | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> SetupOp:
        chosen = [name for name in type(self).model_fields if getattr(self, name) is not None]
        if len(chosen) != 1:
            raise ValueError(f"a setup op names exactly one operation, got {chosen or 'none'}")
        return self

    def apply(self, bank: Bank, today: date, run_prior: PriorRunner) -> None:
        if self.set_kyc_status is not None:
            bank.kyc_status = self.set_kyc_status
        elif self.set_investment_eligible is not None:
            bank.investment_eligible = self.set_investment_eligible
        elif self.prior_execution is not None:
            run_prior(self.prior_execution.plan(), self.prior_execution.minutes_ago)
        else:
            op = self.add_payee or self.add_payment_history or self.set_biller_reference or self.set_balance
            op.apply(bank, today)


def apply_setup(bank: Bank, ops: Sequence[SetupOp], *, today: date, run_prior: PriorRunner) -> None:
    """Apply ops in order. `run_prior` decides how prior payments execute (real gateway or simulated)."""
    for op in ops:
        op.apply(bank, today, run_prior)


# --- Cases ---------------------------------------------------------------------


class Case(_Model):
    """One scripted conversation: setup ops on a fresh seed bank, an optional rogue attack, and the turns."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    tags: list[Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]] = Field(default_factory=list)
    why: str = Field(default="", description="the reasoning behind a non-obvious expectation")
    setup: list[SetupOp] = Field(default_factory=list)
    rogue: str | None = Field(default=None, description="attack the drafter is wrapped with")
    turns: list[CaseTurn] = Field(min_length=1)

    @property
    def benign(self) -> bool:
        return self.rogue is None


def _step_problems(step: StepExpect, view: BankView) -> list[str]:
    """Anything an expected step names that the bank does not have."""
    problems = []
    if step.from_account is not None and step.from_account not in view.accounts:
        problems.append(f"unknown account {step.from_account}")
    match step.type:
        case "own_transfer":
            if step.to not in view.accounts:
                problems.append(f"unknown account {step.to}")
            elif step.to == step.from_account:
                problems.append(f"own_transfer from {step.to} to itself")
        case "paynow":
            if step.to not in view.payees:
                problems.append(f"unknown payee {step.to}")
        case "bill_payment":
            if step.to not in view.billers:
                problems.append(f"unknown biller {step.to}")
        case "equity_buy":
            if view.market.name(step.symbol or "") is None:
                problems.append(f"unlisted symbol {step.symbol}")
    return problems


def _option_problems(option: str, view: BankView) -> list[str]:
    """An expected option must be an amount or an id the bank knows."""
    if as_amount(option) is not None:
        return []
    known = set(view.accounts) | set(view.payees) | set(view.billers)
    if option in known or view.market.name(option) is not None:
        return []
    return [f"option {option!r} is neither an amount nor a bank id"]


def validate_case(case: Case, *, today: date | None = None) -> list[str]:
    """Problems with `case` against a fresh seed bank after its setup; empty when fine."""
    today = today or datetime.now(UTC).date()
    now = datetime.combine(today, time(12), UTC)
    bank = seed_bank(today)

    def simulate(plan: Plan, minutes_ago: int) -> None:
        """Apply a setup payment to the balances only; a real run executes it through the gateway."""
        resolved = resolve(plan, bank.view(), now=now - timedelta(minutes=minutes_ago))
        for account_id, balance in resolved.balances_after.items():
            bank.accounts[account_id].balance = balance

    try:
        apply_setup(bank, case.setup, today=today, run_prior=simulate)
    except (SetupError, EvaluationError) as e:
        return [f"setup: {e}"]

    problems = []
    if case.rogue is not None and case.rogue not in known_attacks():
        problems.append(f"unknown rogue attack {case.rogue!r} (known: {', '.join(known_attacks())})")
    view = bank.view()
    for n, turn in enumerate(case.turns, 1):
        for step in turn.expect.steps:
            problems += [f"turn {n}: {p}" for p in _step_problems(step, view)]
        for option in turn.expect.options:
            problems += [f"turn {n}: {p}" for p in _option_problems(option, view)]
    return problems


def load_cases(path: str | Path) -> list[Case]:
    """Load and validate a case file (a list, or a mapping with a `cases` list)."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    items = raw.get("cases") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise CaseError(f"{path}: expected a non-empty list of cases")

    problems: list[str] = []
    cases: list[Case] = []
    for n, item in enumerate(items, 1):
        label = item.get("id", f"#{n}") if isinstance(item, dict) else f"#{n}"
        try:
            cases.append(Case.model_validate(item))
        except ValidationError as e:
            problems.append(f"{label}: {e}")

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            problems.append(f"{case.id}: duplicate id")
        seen.add(case.id)
        problems += [f"{case.id}: {p}" for p in validate_case(case)]

    if problems:
        raise CaseError(f"{path}: {len(problems)} problem(s)\n" + "\n".join(problems))
    return cases
