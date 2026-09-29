"""Typed transaction plans.

The drafter (an LLM) emits a ``Plan``. A plan is only a proposal: it names
accounts, payees and amounts, and an amount may refer to an earlier step
("buy DBS shares with half of it"). The deterministic evaluator turns a Plan
into a ``ResolvedPlan`` with concrete numbers, and that is what the customer
signs and the gateway executes.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Union

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

StepId = Annotated[str, Field(pattern=r"^t[1-9][0-9]?$")]
Money = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
Balance = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- Amounts -----------------------------------------------------------------


class FixedAmount(_Frozen):
    kind: Literal["fixed"] = "fixed"
    value: Money


class FractionOfStep(_Frozen):
    """An amount defined relative to an earlier step, e.g. "half of it"."""

    kind: Literal["fraction_of_step"] = "fraction_of_step"
    step: StepId
    numerator: int = Field(ge=1)
    denominator: int = Field(ge=1)

    @model_validator(mode="after")
    def _at_most_whole(self) -> FractionOfStep:
        if self.numerator > self.denominator:
            raise ValueError("a fraction of a step cannot exceed the whole step")
        return self


AmountExpr = Annotated[Union[FixedAmount, FractionOfStep], Field(discriminator="kind")]


# --- Draft steps (LLM output) ------------------------------------------------


class _Step(_Frozen):
    id: StepId
    evidence: dict[str, str] = Field(
        default_factory=dict,
        description="field name -> the customer's exact words that justify it",
    )


class OwnTransfer(_Step):
    type: Literal["own_transfer"] = "own_transfer"
    from_account: str
    to_account: str
    amount: AmountExpr


class PayNowTransfer(_Step):
    type: Literal["paynow"] = "paynow"
    from_account: str
    payee_id: str
    amount: AmountExpr


class BillPayment(_Step):
    type: Literal["bill_payment"] = "bill_payment"
    from_account: str
    biller_id: str
    amount: AmountExpr


class EquityBuy(_Step):
    type: Literal["equity_buy"] = "equity_buy"
    funding_account: str
    symbol: str
    notional: AmountExpr


Step = Annotated[
    Union[OwnTransfer, PayNowTransfer, BillPayment, EquityBuy],
    Field(discriminator="type"),
]
StepType = Literal["own_transfer", "paynow", "bill_payment", "equity_buy"]


class Plan(_Frozen):
    steps: list[Step] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def _unique_ids(self) -> Plan:
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        return self


# --- Resolved plan (what the customer signs) ---------------------------------


class EquityFill(_Frozen):
    symbol: str
    quote_id: str
    price: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=4)]
    quantity: int = Field(ge=1, le=10_000_000)
    quote_expires_at: AwareDatetime


class ResolvedStep(_Frozen):
    id: StepId
    type: StepType
    from_account: str
    to: str = Field(description="account id, payee id, biller id or symbol")
    to_display: str = Field(description="name from the bank's own records, never from the LLM")
    amount: Money = Field(description="cash leaving from_account, 2 d.p.")
    equity: EquityFill | None = None

    @model_validator(mode="after")
    def _equity_iff_share_buy(self) -> ResolvedStep:
        if (self.type == "equity_buy") != (self.equity is not None):
            raise ValueError("equity fill must be present exactly for equity_buy steps")
        if self.equity is not None and self.equity.symbol != self.to:
            raise ValueError("equity fill symbol must match the step destination")
        return self


class ResolvedPlan(_Frozen):
    customer_id: str
    steps: tuple[ResolvedStep, ...] = Field(min_length=1, max_length=6)
    balances_after: dict[str, Balance]

    @model_validator(mode="after")
    def _unique_ids(self) -> ResolvedPlan:
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        return self


class Challenge(_Frozen):
    """What the customer's device signs: binds one exact plan to one use."""

    nonce: str
    plan_hash: str
    customer_id: str
    expires_at: AwareDatetime
    script_hash: str | None = Field(
        default=None, description="sha256 of the confirmation script the customer saw/heard"
    )
    policy_decision_id: str | None = None
    step_up_required: bool = Field(
        default=False, description="the gateway's own risk check wants a second factor for this approval"
    )


# --- Conversation ------------------------------------------------------------


class Turn(_Frozen):
    role: Literal["customer", "agent"]
    text: str
    via: Literal["text", "voice"] = "text"  # voice turns may contain speech-to-text slips


class ClarifyOption(_Frozen):
    label: str = Field(description="rendered from bank records")
    value: str = Field(description="e.g. a payee id or an amount")


class Clarification(_Frozen):
    """The agent asks instead of guessing."""

    reason: Literal[
        "ambiguous_payee", "ambiguous_amount", "missing_info", "unsupported", "other"
    ]
    question: str
    options: tuple[ClarifyOption, ...] = ()


# --- Policy and confirmation -------------------------------------------------


class PolicyReason(_Frozen):
    code: str = Field(description="e.g. STEP_UP_AMOUNT, NEW_PAYEE_LIMIT, VELOCITY")
    step_id: str | None = None
    message: str


class PolicyDecision(_Frozen):
    id: str = Field(description="sha256 over the plan hash and the reasons")
    outcome: Literal["allow", "step_up", "hold", "deny"]
    reasons: tuple[PolicyReason, ...] = ()


class ConfirmationView(_Frozen):
    """What the customer sees and hears before signing; never LLM-written."""

    lines: tuple[str, ...]
    spoken_script: str
    script_hash: str
    requires_step_up: bool = False
