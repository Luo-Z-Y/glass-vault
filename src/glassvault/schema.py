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

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

# "t1" to "t99", numbered in the order the customer said the steps.
StepId = Annotated[str, Field(pattern=r"^t[1-9][0-9]?$")]
# A positive S$ amount in whole cents: a fraction of a cent is rejected, never rounded.
Money = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
Balance = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)]


class _Frozen(BaseModel):
    """Immutable, and an unknown field is an error, so nothing extra can ride along with a plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# --- Amounts -----------------------------------------------------------------


class FixedAmount(_Frozen):
    """An S$ amount the customer stated ("a grand" is 1000.00)."""

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


class FractionOfBalance(_Frozen):
    """An amount defined relative to an account balance, e.g. "a quarter of my savings".

    The evaluator uses the balance as it stands when this step runs (after earlier steps).
    """

    kind: Literal["fraction_of_balance"] = "fraction_of_balance"
    account: str
    numerator: int = Field(ge=1)
    denominator: int = Field(ge=1)

    @model_validator(mode="after")
    def _at_most_whole(self) -> FractionOfBalance:
        if self.numerator > self.denominator:
            raise ValueError("a fraction of a balance cannot exceed the whole balance")
        return self


AmountExpr = Annotated[Union[FixedAmount, FractionOfStep, FractionOfBalance], Field(discriminator="kind")]


# --- Draft steps (LLM output) ------------------------------------------------


class _Step(_Frozen):
    """What every draft step carries: its id, and the customer's words behind each field.

    The provenance check freezes a draft whose evidence is not something the customer said.
    """

    id: StepId
    evidence: dict[str, str] = Field(
        default_factory=dict,
        description="field name -> the customer's exact words that justify it",
    )


class OwnTransfer(_Step):
    """Move money between two of the customer's own accounts."""

    type: Literal["own_transfer"] = "own_transfer"
    from_account: str
    to_account: str
    amount: AmountExpr


class PayNowTransfer(_Step):
    """Pay a saved payee by PayNow."""

    type: Literal["paynow"] = "paynow"
    from_account: str
    payee_id: str
    amount: AmountExpr


class BillPayment(_Step):
    """Pay a saved biller."""

    type: Literal["bill_payment"] = "bill_payment"
    from_account: str
    biller_id: str
    amount: AmountExpr


class EquityBuy(_Step):
    """Buy SGX shares with up to `notional` in cash; the evaluator works out how many shares."""

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
    """The drafter's proposal: 1 to 6 steps, in the order the customer said them.

    It is never signed as it stands: the evaluator resolves it into a ResolvedPlan first.
    """

    steps: list[Step] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def _unique_ids(self) -> Plan:
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        return self


# --- Resolved plan (what the customer signs) ---------------------------------


class EquityFill(_Frozen):
    """How a share buy is filled: the quote it uses, at what price, and how many whole shares."""

    symbol: str
    quote_id: str
    price: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=4)]
    quantity: int = Field(ge=1, le=10_000_000)
    quote_expires_at: AwareDatetime


class ResolvedStep(_Frozen):
    """One step with every value fixed.

    `amount` is the exact cash leaving `from_account`. `to_display` is the
    destination's name from bank records; the gateway re-derives it and refuses a mismatch.
    """

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
    """What the customer signs and the gateway executes.

    `balances_after` is what the customer is shown; the gateway refuses to execute
    if, by then, live balances would end up anywhere else.
    """

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
    transcript_hash: str | None = Field(
        default=None, description="sha256 of the conversation this approval came from (crypto.transcript_hash)"
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

    @field_validator("text")
    @classmethod
    def _encodable(cls, text: str) -> str:
        # A lone surrogate is not text: it cannot be hashed into a challenge or written to the log.
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as e:
            raise ValueError("text is not valid Unicode") from e
        return text


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
    """One risk rule that fired, and the step at which its threshold was crossed."""

    code: str = Field(description="e.g. STEP_UP_AMOUNT, NEW_PAYEE_LIMIT, VELOCITY")
    step_id: str | None = None
    message: str


class PolicyDecision(_Frozen):
    """The risk rules' verdict on one exact plan: the most severe outcome, and every rule that fired."""

    id: str = Field(description="sha256 over the plan hash and the reasons")
    outcome: Literal["allow", "step_up", "hold", "deny"]
    reasons: tuple[PolicyReason, ...] = ()


class ConfirmationView(_Frozen):
    """What the customer sees and hears before signing; never LLM-written."""

    lines: tuple[str, ...]
    spoken_script: str
    script_hash: str
    requires_step_up: bool = False


# --- Enquiries (read-only) -----------------------------------------------------


class Enquiry(_Frozen):
    """A read-only question. The AI only picks which one; the bank's records answer it."""

    kind: Literal["balance", "transactions"]
    account: str | None = None
    payee_id: str | None = Field(default=None, description="transactions only: payments to this saved payee")
    limit: int = Field(default=5, ge=1, le=10)


class EnquiryAnswer(_Frozen):
    """What the customer sees and hears for an enquiry: bank records in fixed wording, never LLM text."""

    lines: tuple[str, ...]
    spoken_script: str


# --- Audit records ------------------------------------------------------------

OutcomeKind = Literal["confirm", "clarify", "answer", "frozen", "refused", "held", "error"]
# A record past these limits is refused; the pipeline stops a conversation before it gets there.
MAX_RECORD_TURNS = 100
MAX_RECORD_CHARS = 50_000
MAX_RECORD_BYTES = 256 * 1024


class TraceLine(_Frozen):
    """One stage of the control plane's trace, as the audit log stores it."""

    stage: str = Field(max_length=40)
    ok: bool
    detail: str = Field(max_length=4000)


class DiffLine(_Frozen):
    """One difference between the draft and the validator's reading, as the audit log stores it."""

    kind: Literal["missing", "extra", "changed"]
    step: str = Field(max_length=40)
    detail: str = Field(max_length=1000)


class RequestRecord(_Frozen):
    """One customer turn as the control plane handled it, filed in the gateway's audit log as REQUEST.

    Written for every outcome, so requests the control plane stopped (frozen,
    refused, held) are on record next to the ones that reached a challenge. It
    is the control plane's account of the turn; the gateway checks that a
    confirm record's plan, transcript and spoken script hash to what the signed
    challenge carries. A stopped turn whose full record cannot be filed is filed
    `abridged`: its outcome, its trace and the newest turns that fit.
    """

    transcript: tuple[Turn, ...] = Field(
        max_length=MAX_RECORD_TURNS, description="the turns of this request, with their via flag"
    )
    transcript_dropped: int = Field(
        default=0, ge=0, description="oldest turns left out to fit the limits; never on a confirm record"
    )
    abridged: bool = Field(default=False, description="the full record could not be filed; never on a confirm record")
    outcome: OutcomeKind
    message: str = Field(max_length=4000, description="what the customer was told")
    nonce: str | None = Field(default=None, max_length=64, description="confirm only: the challenge issued")
    draft: Plan | Clarification | Enquiry | None = Field(default=None, description="the drafter's output, unchecked")
    check: Plan | Clarification | None = Field(default=None, description="the validator's own reading, if awaited")
    plan: ResolvedPlan | None = None
    diff: tuple[DiffLine, ...] | None = Field(default=None, max_length=200)
    policy: PolicyDecision | None = None
    clarification: Clarification | None = None
    confirmation: ConfirmationView | None = None
    answer: EnquiryAnswer | None = None
    trace: tuple[TraceLine, ...] = Field(default=(), max_length=50)
    drafter: str | None = Field(default=None, max_length=200, description="the drafter model's name")
    validator: str | None = Field(default=None, max_length=200, description="the validator model's name")
    llm_calls: int = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def _consistent(self) -> RequestRecord:
        if (self.outcome == "confirm") != (self.nonce is not None):
            raise ValueError("a confirm record names its challenge's nonce, and only a confirm record does")
        if self.nonce is not None and (self.transcript_dropped or self.abridged):
            raise ValueError("a confirm record is whole: the transcript its challenge was hashed over, and the rest")
        if sum(len(t.text) for t in self.transcript) > MAX_RECORD_CHARS:
            raise ValueError("transcript too long")
        return self
