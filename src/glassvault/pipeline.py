"""The agent pipeline: a dialogue in, one outcome out.

Two zones, kept apart even while they share a process:

- Agent zone (assume compromised): quarantined bank context -> LLM drafter ->
  a Plan, a clarifying question, or a read-only enquiry that code answers from
  bank records.
- Control plane: deterministic provenance checks, the evaluator's maths, a
  blind validator that re-derives the plan from the dialogue alone, risk rules,
  and the template confirmation the customer signs. Only the control plane asks
  the gateway for a challenge.

Every check that can stop money is deterministic code, except the validator,
which can only stop things (a disagreement freezes; it can never approve).
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol

from .bank import Bank, BankView
from .context import (
    ambiguous_payee_mentions,
    build_context,
    payee_is_mentioned,
    scan_text,
)
from .crypto import canonical_bytes, transcript_hash
from .drafter import DraftError, draft
from .evaluator import EvaluationError, resolve
from .gateway import Execution, Gateway, GatewayError
from .llm import LLM, LLMResponse
from .numbers import confusable_reasons, extract_amounts, extract_fractions
from .policy import evaluate_policy
from .renderer import RenderError, render
from .schema import (
    BillPayment,
    Challenge,
    Clarification,
    ClarifyOption,
    MAX_RECORD_CHARS,
    MAX_RECORD_TURNS,
    ConfirmationView,
    DiffLine,
    Enquiry,
    EnquiryAnswer,
    EquityBuy,
    FixedAmount,
    FractionOfBalance,
    FractionOfStep,
    OutcomeKind,
    OwnTransfer,
    PayNowTransfer,
    Plan,
    PolicyDecision,
    RequestRecord,
    ResolvedPlan,
    TraceLine,
    Turn,
)
from .enquiry import answer as answer_enquiry
from .validator import PlanDiff, diff_plans, independent_plan

# Customer-facing messages are fixed templates, never LLM text.
MSG_FROZEN = "I've stopped this request because a safety check didn't match what you asked for. Nothing has been sent."
MSG_INJECTION = (
    "That sounds like instructions copied from a message. For your safety I won't act on it. "
    "If you want to make a payment, tell me in your own words."
)
MSG_HELD = "For your protection this is on hold and nothing has been sent: {reasons}"
MSG_REFUSED = "I can't do this: {reasons}"
MSG_CONFIRM = "Please check the details and confirm with Face ID."
MSG_ERROR = "Sorry, I couldn't process that. Nothing has been sent. Could you say it another way?"
MSG_TOO_LONG = "This conversation has got too long for me to follow safely. Nothing has been sent. Please start again."

# How much transcript (canonical JSON) a record may carry, well inside the gateway's per-record cap.
RECORD_TRANSCRIPT_BYTES = 128 * 1024
ABRIDGED_TRANSCRIPT_BYTES = 64 * 1024


@dataclass(frozen=True)
class TraceEvent:
    """One line in the live security console."""

    stage: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Outcome:
    """The result of one customer turn; `kind` decides which other fields are set.

    confirm: plan, confirmation, policy and challenge. clarify: clarification (and the
    plan, when the validator is the one asking). answer: answer, read from bank records;
    there is no plan or challenge.
    frozen, refused and held carry the plan if it got that far, and `trace` always
    ends with the stage that decided.
    """

    kind: OutcomeKind
    message: str
    plan: ResolvedPlan | None = None
    clarification: Clarification | None = None
    confirmation: ConfirmationView | None = None
    policy: PolicyDecision | None = None
    challenge: Challenge | None = None
    diff: PlanDiff | None = None
    answer: EnquiryAnswer | None = None  # kind "answer": a read-only enquiry, from bank records
    trace: tuple[TraceEvent, ...] = ()
    llm_calls: int = 0


class GatewayPort(Protocol):
    """What the pipeline may ask of the gateway: read state, request a challenge, file a turn. Never execute."""

    def now(self) -> datetime: ...
    def view(self) -> BankView: ...
    def history(self, since: datetime | None = None) -> Sequence[Execution]: ...
    def issue_challenge(
        self, plan: ResolvedPlan, *, script_hash: str | None = None, transcript_hash: str | None = None,
        policy_decision_id: str | None = None,
    ) -> Challenge: ...
    def record_request(self, record: RequestRecord) -> Any: ...


class LocalGatewayPort:
    """In-process port for the web demo and the benchmark; `GatewayClient` (services/gateway_api.py) is the HTTP one."""

    def __init__(self, gateway: Gateway):
        self._gateway = gateway

    def now(self) -> datetime:
        return self._gateway.now()

    def view(self) -> BankView:
        return self._gateway.bank.view()

    def history(self, since: datetime | None = None) -> Sequence[Execution]:
        return self._gateway.history(since)

    def issue_challenge(
        self, plan: ResolvedPlan, *, script_hash: str | None = None, transcript_hash: str | None = None,
        policy_decision_id: str | None = None,
    ) -> Challenge:
        return self._gateway.issue_challenge(
            plan, script_hash=script_hash, transcript_hash=transcript_hash, policy_decision_id=policy_decision_id
        )

    def record_request(self, record: RequestRecord) -> Any:
        return self._gateway.record_request(record)


class _CountingLLM:
    """Counts model calls, for `Outcome.llm_calls` and the benchmark's cost figures."""

    def __init__(self, llm: LLM):
        self._llm = llm
        self.name = llm.name
        self.calls = 0

    def chat(self, messages: list[dict], **kwargs: Any) -> LLMResponse:
        self.calls += 1
        return self._llm.chat(messages, **kwargs)


@dataclass
class _Drafts:
    """What the two models produced on this turn, kept for its REQUEST record."""

    draft: Plan | Clarification | Enquiry | None = None
    check: Plan | Clarification | None = None


class _Stop(Exception):
    """Raised inside the control plane to end the turn with a given outcome."""

    def __init__(self, kind: OutcomeKind, message: str, **extra: Any):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.extra = extra


class Pipeline:
    """The agent zone and the control plane. It reaches the gateway only through a GatewayPort, which cannot execute."""

    def __init__(self, gateway: GatewayPort, drafter_llm: LLM, validator_llm: LLM):
        self._gateway = gateway
        self._drafter_llm = drafter_llm
        self._validator_llm = validator_llm

    def handle(self, dialogue: Sequence[Turn]) -> Outcome:
        """Take the dialogue so far and return one Outcome for the customer's latest turn.

        A stopped request comes back as an Outcome, not an exception, and its trace
        shows which stage stopped it. Whatever the outcome, the turn is filed in the
        gateway's audit log as REQUEST before it is returned. If that fails, the turn
        ends in an error and nothing is offered for signing.
        """
        dialogue = _current_request(dialogue)
        drafter, validator = _CountingLLM(self._drafter_llm), _CountingLLM(self._validator_llm)
        trace: list[TraceEvent] = []
        drafts = _Drafts()
        try:
            if not _fits(dialogue, RECORD_TRANSCRIPT_BYTES):
                # Too long to keep whole on record, so nothing from it may be signed.
                trace.append(TraceEvent("current-request", False, f"{len(dialogue)} turns: too long to keep on record"))
                raise _Stop("error", MSG_TOO_LONG)
            outcome = self._handle(dialogue, drafter, validator, trace, drafts)
        except _Stop as stop:
            outcome = Outcome(stop.kind, stop.message, trace=tuple(trace),
                              llm_calls=drafter.calls + validator.calls, **stop.extra)
        except Exception as e:
            # A bug, not a decision: it still goes on record, then propagates as before.
            trace.append(TraceEvent("error", False, f"unexpected {type(e).__name__}"))
            with contextlib.suppress(Exception):
                self._record(dialogue, Outcome("error", MSG_ERROR, trace=tuple(trace),
                                               llm_calls=drafter.calls + validator.calls), drafts)
            raise
        try:
            self._record(dialogue, outcome, drafts)
        except Exception as e:
            # No record, no offer: a challenge already issued is never shown, so it expires unused.
            reason = getattr(e, "code", None) or type(e).__name__
            failed = TraceEvent("audit", False, f"could not file the request ({reason}); nothing was offered")
            outcome = Outcome("error", MSG_ERROR, trace=outcome.trace + (failed,), llm_calls=outcome.llm_calls)
            if _refused(e):  # after a lost connection the record may have landed; do not file it twice
                with contextlib.suppress(Exception):
                    self._gateway.record_request(self._abridged(dialogue, outcome))
        return outcome

    def _record(self, dialogue: Sequence[Turn], outcome: Outcome, drafts: _Drafts) -> None:
        """File this turn in the gateway's audit log as a REQUEST entry (see RequestRecord).

        A confirm is filed whole or not at all. A stopped turn whose full record cannot be
        built or filed (too big, or model text the log cannot hold) is filed abridged instead.
        """
        try:
            self._gateway.record_request(self._full(dialogue, outcome, drafts))
        except Exception as e:
            if outcome.kind == "confirm" or not _refused(e):
                raise
            self._gateway.record_request(self._abridged(dialogue, outcome))

    def _abridged(self, dialogue: Sequence[Turn], outcome: Outcome) -> RequestRecord:
        """The outcome, the trace and the newest turns that fit, with any text the log cannot hold replaced."""
        kept = _fitted(dialogue, ABRIDGED_TRANSCRIPT_BYTES)
        return RequestRecord(
            transcript=tuple(kept),
            transcript_dropped=len(dialogue) - len(kept),
            abridged=True,
            outcome=outcome.kind,
            message=_loggable(outcome.message)[:1000],
            trace=tuple(TraceLine(stage=_loggable(t.stage)[:40], ok=t.ok, detail=_loggable(t.detail)[:500])
                        for t in outcome.trace[:50]),
            drafter=_loggable(self._drafter_llm.name)[:200],
            validator=_loggable(self._validator_llm.name)[:200],
            llm_calls=min(outcome.llm_calls, 100),
        )

    def _full(self, dialogue: Sequence[Turn], outcome: Outcome, drafts: _Drafts) -> RequestRecord:
        """Everything about the turn; a stopped over-long request keeps its newest turns that fit."""
        diff = outcome.diff.diffs if outcome.diff else None
        kept = _fitted(dialogue, RECORD_TRANSCRIPT_BYTES)
        return RequestRecord(
            transcript=tuple(kept),
            transcript_dropped=len(dialogue) - len(kept),
            outcome=outcome.kind,
            message=outcome.message[:4000],
            nonce=outcome.challenge.nonce if outcome.challenge else None,
            draft=drafts.draft,
            check=drafts.check,
            plan=outcome.plan,
            diff=None if diff is None else tuple(DiffLine(kind=d.kind, step=d.step, detail=d.detail[:1000]) for d in diff),
            policy=outcome.policy,
            clarification=outcome.clarification,
            confirmation=outcome.confirmation,
            answer=outcome.answer,
            trace=tuple(TraceLine(stage=t.stage[:40], ok=t.ok, detail=t.detail[:4000]) for t in outcome.trace),
            drafter=self._drafter_llm.name[:200],
            validator=self._validator_llm.name[:200],
            llm_calls=outcome.llm_calls,
        )

    def _handle(self, dialogue: Sequence[Turn], drafter: _CountingLLM, validator: _CountingLLM,
                trace: list[TraceEvent], drafts: _Drafts) -> Outcome:
        view = self._gateway.view()
        now = self._gateway.now()
        said = _customer_text(dialogue)

        signals = scan_text(said)
        if signals:
            trace.append(TraceEvent("injection-scan", False, ", ".join(sorted({s.kind for s in signals}))))
            raise _Stop("refused", MSG_INJECTION)
        trace.append(TraceEvent("injection-scan", True, "no instruction-like text in the customer's words"))

        context = build_context(view)
        trace.append(TraceEvent("quarantine", True, f"{len(context.placeholders)} untrusted text field(s) replaced by placeholders"))

        # The validator never sees the draft, so it can run at the same time as the drafter.
        # The pool is not waited on: a question or an enquiry returns without the validator.
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            draft_future = pool.submit(draft, dialogue, context, drafter)
            check_future = pool.submit(independent_plan, dialogue, context, validator)
            try:
                drafted = draft_future.result()
            except DraftError as e:
                trace.append(TraceEvent("draft", False, str(e)))
                raise _Stop("error", MSG_ERROR) from e
            drafts.draft = drafted

            if isinstance(drafted, Clarification):
                trace.append(TraceEvent("draft", True, f"asks: {drafted.reason}"))
                raise _Stop("clarify", drafted.question, clarification=drafted)
            if isinstance(drafted, Enquiry):
                return self._answer(drafted, view, trace, drafter, validator)
            trace.append(TraceEvent("draft", True, f"{len(drafted.steps)} step(s) drafted"))

            self._check_provenance(drafted, dialogue, view, trace)
            try:
                check = check_future.result()
            except DraftError as e:
                trace.append(TraceEvent("validator", False, f"validator failed: {e}"))
                raise _Stop("frozen", MSG_FROZEN) from e
            drafts.check = check
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        # Resolve only now, so a share quote's 60-second clock starts after the LLM calls.
        plan = self._resolve(drafted, view, now, trace, stage="resolve")

        if isinstance(check, Clarification):
            trace.append(TraceEvent("validator", False, f"validator would ask first: {check.reason}"))
            raise _Stop("clarify", check.question, clarification=check, plan=plan)
        try:
            check_plan = resolve(check, view, now=now)
        except EvaluationError as e:
            trace.append(TraceEvent("validator", False, f"validator's plan does not resolve: {e.code}"))
            raise _Stop("frozen", MSG_FROZEN, plan=plan) from e
        diff = diff_plans(plan, check_plan)
        if not diff.matches:
            trace.append(TraceEvent("validator", False, "; ".join(d.detail for d in diff.diffs)))
            raise _Stop("frozen", MSG_FROZEN, plan=plan, diff=diff)
        trace.append(TraceEvent("validator", True, "independent re-derivation matches the draft"))
        # After the comparison: a rogue draft that swaps the source is frozen above; this catches a
        # default that both models got wrong the same way.
        self._check_sources(drafted, _agent_text(dialogue), view, trace)

        decision = evaluate_policy(plan, view, now=now, history=self._gateway.history(now - timedelta(days=7)))
        reasons = "; ".join(r.message for r in decision.reasons)
        trace.append(TraceEvent("policy", decision.outcome in ("allow", "step_up"), f"{decision.outcome}: {reasons or 'no rules triggered'}"))
        if decision.outcome == "deny":
            raise _Stop("refused", MSG_REFUSED.format(reasons=reasons), plan=plan, policy=decision)
        if decision.outcome == "hold":
            raise _Stop("held", MSG_HELD.format(reasons=reasons), plan=plan, policy=decision)

        try:
            confirmation = render(plan, decision, view)
        except RenderError as e:
            # A plan the customer cannot be shown truthfully must not be signed. Logged under "challenge",
            # the stage the security console shows as the bank-rendered confirmation.
            trace.append(TraceEvent("challenge", False, f"renderer refused: {e}"))
            raise _Stop("frozen", MSG_FROZEN, plan=plan) from e
        try:
            challenge = self._gateway.issue_challenge(
                plan, script_hash=confirmation.script_hash, transcript_hash=transcript_hash(dialogue),
                policy_decision_id=decision.id,
            )
        except GatewayError as e:
            # The gateway re-checks everything; if it disagrees with us, stop.
            trace.append(TraceEvent("challenge", False, f"gateway refused: {e.code}"))
            kind: OutcomeKind = "refused" if e.code == "INSUFFICIENT_FUNDS" else "frozen"
            raise _Stop(kind, MSG_FROZEN if kind == "frozen" else MSG_REFUSED.format(reasons=e.message), plan=plan) from e
        trace.append(TraceEvent("challenge", True, f"awaiting the customer's signature (nonce {challenge.nonce[:8]}…)"))
        return Outcome(
            "confirm", MSG_CONFIRM, plan=plan, confirmation=confirmation, policy=decision,
            challenge=challenge, trace=tuple(trace), llm_calls=drafter.calls + validator.calls,
        )

    def _answer(self, enquiry: Enquiry, view: BankView, trace: list[TraceEvent],
                drafter: _CountingLLM, validator: _CountingLLM) -> Outcome:
        """Read-only: answered from bank records in fixed wording; no plan, no challenge, no money moves."""
        target = view.accounts[enquiry.account].name if enquiry.account in view.accounts else "all accounts"
        trace.append(TraceEvent("draft", True, f"enquiry: {enquiry.kind} ({target})"))
        result = answer_enquiry(enquiry, view, self._gateway.history())
        trace.append(TraceEvent("records", True, f"{len(result.lines)} line(s) from bank records; nothing to sign"))
        return Outcome("answer", "\n".join(result.lines), answer=result, trace=tuple(trace),
                       llm_calls=drafter.calls + validator.calls)

    # --- control-plane checks ------------------------------------------------

    def _resolve(self, plan: Plan, view: BankView, now: datetime, trace: list[TraceEvent], *, stage: str) -> ResolvedPlan:
        """Resolve the draft, turning an EvaluationError into the outcome the customer should get.

        Not enough money is refused and too little for one share is a question; any
        other error (an unknown account, payee, biller, share or step, a transfer to the same
        account, a zero amount) means the draft cannot be carried out as written, so it is frozen.
        """
        try:
            resolved = resolve(plan, view, now=now)
        except EvaluationError as e:
            trace.append(TraceEvent(stage, False, str(e)))
            if e.code == "INSUFFICIENT_FUNDS":
                raise _Stop("refused", MSG_REFUSED.format(reasons=e.message)) from e
            if e.code == "NOTIONAL_TOO_SMALL":
                raise _Stop("clarify", f"{e.message}. How much would you like to invest?",
                            clarification=Clarification(reason="ambiguous_amount", question=e.message)) from e
            # Anything else means the draft cannot be carried out as written.
            raise _Stop("frozen", MSG_FROZEN) from e
        trace.append(TraceEvent(stage, True, f"amounts computed in code; balances checked step by step"))
        return resolved

    def _check_provenance(self, plan: Plan, dialogue: Sequence[Turn], view: BankView, trace: list[TraceEvent]) -> None:
        """Every amount, payee and destination must trace back to the customer's own words."""
        said = _customer_text(dialogue)
        asked = _agent_text(dialogue)
        amounts = _live_amounts(dialogue)
        spoken_amounts = {m.value for t in dialogue if t.role == "customer" and t.via == "voice"
                          for m in extract_amounts(t.text)}
        fractions = {(f.numerator, f.denominator) for f in extract_fractions(said)}

        def freeze(detail: str) -> None:
            trace.append(TraceEvent("provenance", False, detail))
            raise _Stop("frozen", MSG_FROZEN)

        def ask(detail: str, clarification: Clarification) -> None:
            trace.append(TraceEvent("provenance", False, detail))
            raise _Stop("clarify", clarification.question, clarification=clarification)

        for step in plan.steps:
            for field, quote in step.evidence.items():
                if quote and _norm(quote) not in _norm(said):
                    freeze(f"{step.id}: evidence for {field} ({quote!r}) is not something the customer said")

            expr = step.notional if isinstance(step, EquityBuy) else step.amount
            if isinstance(expr, FixedAmount) and expr.value not in amounts:
                bill_due = (
                    isinstance(step, BillPayment)
                    and step.biller_id in view.billers
                    and expr.value == view.billers[step.biller_id].amount_due
                )
                if not bill_due:
                    # The drafter reads the same transcript we do, so it cannot mishear it:
                    # an amount the customer never said is never turned into a question.
                    freeze(f"{step.id}: S${expr.value:,.2f} was never mentioned by the customer")
            if isinstance(expr, FractionOfStep) and (expr.numerator, expr.denominator) not in fractions:
                freeze(f"{step.id}: fraction {expr.numerator}/{expr.denominator} was never mentioned")
            if isinstance(expr, FractionOfBalance):
                if (expr.numerator, expr.denominator) not in fractions:
                    freeze(f"{step.id}: fraction {expr.numerator}/{expr.denominator} was never mentioned")
                if expr.account in view.accounts and not _name_mentioned(view.accounts[expr.account].name, said):
                    freeze(f"{step.id}: the customer never mentioned the {view.accounts[expr.account].name}")

            match step:
                case PayNowTransfer() if step.payee_id in view.payees:
                    payee = view.payees[step.payee_id]
                    if not payee_is_mentioned(step.payee_id, said, view):
                        freeze(f"{step.id}: the customer never mentioned {payee.legal_name}")
                    for phrase, ids in ambiguous_payee_mentions(said, view):
                        if step.payee_id in ids and not any(view.payees[i].legal_name in asked for i in ids if i in view.payees):
                            ask(f"{step.id}: '{phrase}' could mean {len(ids)} payees",
                                _which_payee(phrase, ids, view))
                    if isinstance(step.amount, FixedAmount) and step.amount.value in spoken_amounts:
                        said_value = step.amount.value
                        alt = _likelier_reading(said_value, payee)
                        if alt is not None and not _mentions_amount(asked, said_value):
                            ask(f"{step.id}: speech-to-text may have misheard S${alt:,.2f} as S${said_value:,.2f}",
                                Clarification(
                                    reason="ambiguous_amount",
                                    question=f"Did you say S${said_value:,.2f} or S${alt:,.2f}?",
                                    options=(ClarifyOption(label=f"S${said_value:,.2f}", value=str(said_value)),
                                             ClarifyOption(label=f"S${alt:,.2f}", value=str(alt))),
                                ))
                    if isinstance(step.amount, FixedAmount) and _is_outlier(step.amount.value, payee) \
                            and not _mentions_amount(asked, step.amount.value):
                        usual = max(p.amount for p in payee.history)
                        ask(f"{step.id}: S${step.amount.value:,.2f} is far above the usual S${usual:,.2f}",
                            Clarification(
                                reason="ambiguous_amount",
                                question=(f"Just to be sure: S${step.amount.value:,.2f} to {payee.legal_name}? "
                                          f"You usually send about S${usual:,.2f}."),
                                options=(ClarifyOption(label=f"Yes, S${step.amount.value:,.2f}", value=str(step.amount.value)),
                                         ClarifyOption(label="No, cancel", value="cancel")),
                            ))
                case BillPayment() if step.biller_id in view.billers:
                    if not (_name_mentioned(view.billers[step.biller_id].name, said) or _ALL_BILLS.search(said)):
                        freeze(f"{step.id}: the customer never mentioned {view.billers[step.biller_id].name}")
                case OwnTransfer() if step.to_account in view.accounts:
                    if not _name_mentioned(view.accounts[step.to_account].name, said):
                        freeze(f"{step.id}: the customer never mentioned the {view.accounts[step.to_account].name}")
                case EquityBuy():
                    listing = view.market.name(step.symbol)
                    if listing and not (_name_mentioned(listing, said) or re.search(rf"\b{re.escape(step.symbol)}\b", said, re.I)):
                        freeze(f"{step.id}: the customer never mentioned {listing}")
        trace.append(TraceEvent("provenance", True, "every amount, payee and destination traces to the customer's words"))

    def _check_sources(self, plan: Plan, asked: str, view: BankView, trace: list[TraceEvent]) -> None:
        """Share buys and "half of it" steps with no stated source pay from the account an earlier step tops up.

        That is the product's default for "move 1000 to joint, then buy 500 dollars of DBS". A draft that
        breaks it gets a fixed-wording question instead of a silent guess. Bills and PayNow for their own
        amounts keep the usual default.
        """
        topped_up: list[str] = []
        transfers: dict[str, str] = {}  # own-transfer step id -> account it pays into
        for step in plan.steps:
            if isinstance(step, OwnTransfer):
                topped_up.append(step.to_account)
                transfers[step.id] = step.to_account
                continue
            source = step.funding_account if isinstance(step, EquityBuy) else step.from_account
            expr = step.notional if isinstance(step, EquityBuy) else step.amount
            spends_transfer = isinstance(expr, FractionOfStep) and expr.step in transfers
            rule_applies = isinstance(step, EquityBuy) or spends_transfer
            stated = bool(step.evidence.get("from_account", "").strip())
            if stated or not topped_up or not rule_applies or source == topped_up[-1] or SOURCE_QUESTION in asked:
                continue
            if source not in view.accounts or topped_up[-1] not in view.accounts:
                continue  # the evaluator reports unknown accounts
            usual, target = view.accounts[source], view.accounts[topped_up[-1]]
            question = (f"Should I pay for {_describe(step, view)} from your {target.name}, "
                        f"{SOURCE_QUESTION}, or from your {usual.name}?")
            trace.append(TraceEvent("provenance", False,
                                    f"{step.id}: source not stated; drafted from {usual.name}, "
                                    f"but this request tops up the {target.name}"))
            raise _Stop("clarify", question, clarification=Clarification(
                reason="missing_info", question=question,
                options=(ClarifyOption(label=target.name, value=target.id),
                         ClarifyOption(label=usual.name, value=usual.id)),
            ))


# --- helpers -------------------------------------------------------------------

# Part of the fixed question `_check_sources` asks. Finding it in the agent's turns
# means the customer was already asked, so the question is not repeated.
SOURCE_QUESTION = "which you're moving money into"


def _describe(step: Any, view: BankView) -> str:
    """How the source-account question names a step, from bank records."""
    match step:
        case EquityBuy():
            return f"the {view.market.name(step.symbol) or step.symbol} shares"
        case PayNowTransfer() if step.payee_id in view.payees:
            return f"the PayNow to {view.payees[step.payee_id].legal_name}"
        case BillPayment() if step.biller_id in view.billers:
            return f"the {view.billers[step.biller_id].name} bill"
    return f"step {step.id}"

# Too common in bank-record names to count as naming one: "Joint Account" needs "joint".
_GENERIC_WORDS = {"account", "group", "holdings", "bank", "the", "my", "of", "and", "ltd", "limited"}


def _customer_text(dialogue: Sequence[Turn]) -> str:
    return "\n".join(t.text for t in dialogue if t.role == "customer")


def _agent_text(dialogue: Sequence[Turn]) -> str:
    return "\n".join(t.text for t in dialogue if t.role == "agent")


def _norm(text: str) -> str:
    """How evidence is matched against the transcript: lower case, punctuation other than $.,% as spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s$.,%]", " ", text.lower())).strip()


def _name_mentioned(name: str, said: str) -> bool:
    """True if a distinctive word of a bank-record name appears in what the customer said."""
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in _GENERIC_WORDS and len(w) >= 2]
    return any(re.search(rf"\b{re.escape(w)}\b", said, re.I) for w in words)


def _mentions_amount(text: str, value: Decimal) -> bool:
    """True if the agent already asked about this exact amount, so the customer is not asked twice."""
    return f"S${value:,.2f}" in text


# An agent turn reporting that a request went through; everything up to it is finished business.
_EXECUTED = re.compile(r"\bhas been executed\b|^Done\b", re.I)
# "Both my bills", "all of my bills": every saved biller at once.
_ALL_BILLS = re.compile(r"\b(both|all)\s+(of\s+)?(my\s+)?bills\b", re.I)


def _fits(turns: Sequence[Turn], budget: int) -> bool:
    """True if a record can hold these turns whole: the turn and character limits, and `budget` bytes."""
    if len(turns) > MAX_RECORD_TURNS or sum(len(t.text) for t in turns) > MAX_RECORD_CHARS:
        return False
    return len(canonical_bytes([t.model_dump(mode="json") for t in turns])) <= budget


def _fitted(turns: Sequence[Turn], budget: int) -> list[Turn]:
    """The newest turns that fit in `budget` bytes and the record's turn and character limits."""
    kept = list(turns)
    while kept and not _fits(kept, budget):
        kept.pop(0)
    return kept


def _refused(error: Exception) -> bool:
    """True if a record could not be built or the gateway turned it down, as opposed to a lost connection."""
    if isinstance(error, ValueError):  # building it locally: pydantic's ValidationError, UnicodeError
        return True
    return getattr(error, "code", None) not in (None, "HTTP_ERROR")  # a gateway refusal has a code


def _loggable(text: str) -> str:
    """`text` with anything that is not valid Unicode (a lone surrogate from a model) replaced."""
    return text.encode("utf-8", "replace").decode("utf-8")


def _current_request(dialogue: Sequence[Turn]) -> list[Turn]:
    """Only the turns since the last executed request.

    Money already sent must not count as "said" for the next request, or a drafter
    could repeat a payment and still pass provenance and the validator.
    """
    last_done = max((i for i, t in enumerate(dialogue) if t.role == "agent" and _EXECUTED.search(t.text)),
                    default=-1)
    return list(dialogue[last_done + 1:])


# In a reply to the agent's question, these words take back the amounts it asked about.
_NEGATION = re.compile(r"\b(no|nope|not|wrong|cancel|instead|meant)\b", re.I)


def _live_amounts(dialogue: Sequence[Turn]) -> set[Decimal]:
    """Amounts the customer said and did not later take back.

    When the agent asks about amounts and the customer's reply names a different
    amount or says no, the asked amounts no longer count as said
    ("S$6,000 to Mum?" -> "sorry, I meant six hundred").
    """
    live: set[Decimal] = set()
    for i, turn in enumerate(dialogue):
        values = {m.value for m in extract_amounts(turn.text)}
        if turn.role == "customer":
            live |= values
            continue
        reply = next((t for t in dialogue[i + 1:] if t.role == "customer"), None)
        if reply is None or not values:
            continue
        reply_values = {m.value for m in extract_amounts(reply.text)}
        if (reply_values and not reply_values & values) or _NEGATION.search(reply.text):
            live -= values
    return live


def _likelier_reading(value: Decimal, payee: Any) -> Decimal | None:
    """A teen/ty mishearing of `value` that is much closer to what this payee usually gets."""
    if not payee.history:
        return None
    usual = max(p.amount for p in payee.history)
    for alt, reason in confusable_reasons(value).items():
        if reason == "teen_ty" and abs(alt - usual) * 3 < abs(value - usual):
            return alt
    return None


def _is_outlier(amount: Decimal, payee: Any) -> bool:
    """More than 10 times this payee's largest past payment (the multiple the risk rules also use)."""
    return bool(payee.history) and amount > 10 * max(p.amount for p in payee.history)


def _which_payee(phrase: str, ids: Sequence[str], view: BankView) -> Clarification:
    """A "which payee?" question from bank records, with each payee's last payment to tell them apart."""
    options = []
    for payee_id in ids:
        payee = view.payees[payee_id]
        if payee.history:
            last = max(payee.history, key=lambda p: p.on)
            seen = f"last paid S${last.amount:,.2f} on {last.on:%d %b}"
        else:
            seen = "never paid"
        options.append(ClarifyOption(label=f"{payee.legal_name} ({payee.paynow_proxy}, {seen})", value=payee_id))
    names = " or ".join(o.label for o in options)
    return Clarification(reason="ambiguous_payee", question=f"Which {phrase} do you mean: {names}?", options=tuple(options))


# --- benchmark adapter -----------------------------------------------------------


def agent_factory(
    bank: Bank,
    gateway: Gateway,
    attack: str | None,
    *,
    drafter_llm: LLM | None = None,
    validator_llm: LLM | None = None,
) -> Callable[[Sequence[Turn]], Any]:
    """Glass Vault as an `evals.harness.AgentFactory`. With `attack`, the drafter is made rogue."""
    from .evals.harness import AgentOutcome
    from .llm import llm_from_env
    from .rogue import RogueLLM

    drafter = drafter_llm or llm_from_env("drafter")
    validator = validator_llm or llm_from_env("validator")
    if attack:
        drafter = RogueLLM(drafter, attack, bank.view())
    pipeline = Pipeline(LocalGatewayPort(gateway), drafter, validator)

    def run(dialogue: Sequence[Turn]) -> AgentOutcome:
        outcome = pipeline.handle(dialogue)
        return AgentOutcome(
            kind=outcome.kind,
            plan=outcome.plan if outcome.kind == "confirm" else None,
            clarification=outcome.clarification,
            detail="; ".join(f"{e.stage}: {e.detail}" for e in outcome.trace if not e.ok) or outcome.message,
            llm_calls=outcome.llm_calls,
            step_up=(outcome.policy.outcome == "step_up") if outcome.policy else None,
            challenge=outcome.challenge,
            answer_lines=outcome.answer.lines if outcome.answer else (),
        )

    return run
