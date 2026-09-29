"""Deterministic evaluator: Plan -> ResolvedPlan.

All money arithmetic happens here, in Decimal, never inside the LLM. Steps run
in order against a simulated copy of the balances, so "move a grand to joint,
then buy shares with half of it" is checked with the joint balance *after* the
first transfer.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from .bank import BankView, share_cost
from .schema import (
    AmountExpr,
    BillPayment,
    EquityBuy,
    EquityFill,
    FixedAmount,
    FractionOfBalance,
    OwnTransfer,
    PayNowTransfer,
    Plan,
    ResolvedPlan,
    ResolvedStep,
    StepType,
)

CENT = Decimal("0.01")


def destination_label(records: Any, step_type: StepType, to: str, equity: EquityFill | None) -> str | None:
    """What the customer is shown as the destination, from bank records only.

    `records` is a Bank or BankView. The gateway calls this too and rejects any
    plan whose `to_display` differs, so an agent can never show one payee and pay another.
    """
    match step_type:
        case "own_transfer":
            account = records.accounts.get(to)
            return account.name if account else None
        case "paynow":
            payee = records.payees.get(to)
            return f"{payee.legal_name} ({payee.paynow_proxy})" if payee else None
        case "bill_payment":
            biller = records.billers.get(to)
            # The biller's free-text reference is untrusted, so it is never displayed.
            return biller.name if biller else None
        case "equity_buy":
            name = records.market.name(to)
            if name is None or equity is None or equity.symbol != to:
                return None
            return f"{name} ({to}) x {equity.quantity} @ S${equity.price}"
    return None


class EvaluationError(Exception):
    """The plan cannot be carried out as written; `code` says why (listed in docs/CONTRACTS.md)."""

    def __init__(self, code: str, message: str, step_id: str | None = None):
        super().__init__(f"{code}: {message}" + (f" (step {step_id})" if step_id else ""))
        self.code = code
        self.message = message
        self.step_id = step_id


def resolve(plan: Plan, view: BankView, *, now: datetime) -> ResolvedPlan:
    """Work out every amount in `plan`, running its steps in order against a copy of the balances.

    Each share buy takes a fresh quote from `view.market`, so resolving the same
    plan twice gives different quote ids. The balances in `view` are not touched.
    Raises EvaluationError if a step names something unknown or cannot be paid for.
    """
    balances = {a.id: a.balance for a in view.accounts.values()}
    cash_by_step: dict[str, Decimal] = {}
    resolved: list[ResolvedStep] = []

    def account(account_id: str, step_id: str) -> None:
        if account_id not in balances:
            raise EvaluationError("UNKNOWN_ACCOUNT", f"no account {account_id!r}", step_id)

    def debit(account_id: str, amount: Decimal, step_id: str) -> None:
        if balances[account_id] < amount:
            short = amount - balances[account_id]
            raise EvaluationError(
                "INSUFFICIENT_FUNDS", f"{account_id} is short by S${short:,.2f}", step_id
            )
        balances[account_id] -= amount

    for step in plan.steps:
        match step:
            case OwnTransfer():
                account(step.from_account, step.id)
                account(step.to_account, step.id)
                if step.from_account == step.to_account:
                    raise EvaluationError("SAME_ACCOUNT", "cannot transfer to the same account", step.id)
                cash = _amount(step.amount, cash_by_step, balances, step.id)
                debit(step.from_account, cash, step.id)
                balances[step.to_account] += cash
                to, equity = step.to_account, None

            case PayNowTransfer():
                account(step.from_account, step.id)
                payee = view.payees.get(step.payee_id)
                if payee is None:
                    raise EvaluationError("UNKNOWN_PAYEE", f"no saved payee {step.payee_id!r}", step.id)
                cash = _amount(step.amount, cash_by_step, balances, step.id)
                debit(step.from_account, cash, step.id)
                to, equity = payee.id, None

            case BillPayment():
                account(step.from_account, step.id)
                biller = view.billers.get(step.biller_id)
                if biller is None:
                    raise EvaluationError("UNKNOWN_BILLER", f"no saved biller {step.biller_id!r}", step.id)
                cash = _amount(step.amount, cash_by_step, balances, step.id)
                debit(step.from_account, cash, step.id)
                to, equity = biller.id, None

            case EquityBuy():
                account(step.funding_account, step.id)
                quote = view.market.quote(step.symbol, now)
                if quote is None:
                    raise EvaluationError("UNKNOWN_SYMBOL", f"no listing {step.symbol!r}", step.id)
                notional = _amount(step.notional, cash_by_step, balances, step.id)
                quantity = int(notional // quote.price)
                if quantity < 1:
                    raise EvaluationError(
                        "NOTIONAL_TOO_SMALL",
                        f"S${notional:,.2f} does not buy one share at S${quote.price}",
                        step.id,
                    )
                cash = share_cost(quote.price, quantity)
                debit(step.funding_account, cash, step.id)
                to = step.symbol
                equity = EquityFill(
                    symbol=step.symbol,
                    quote_id=quote.quote_id,
                    price=quote.price,
                    quantity=quantity,
                    quote_expires_at=quote.expires_at,
                )

            case _:  # pragma: no cover - the Step union is exhaustive
                raise EvaluationError("UNKNOWN_STEP", f"unsupported step {step!r}")

        from_account = step.funding_account if isinstance(step, EquityBuy) else step.from_account
        cash_by_step[step.id] = cash
        resolved.append(
            ResolvedStep(
                id=step.id,
                type=step.type,
                from_account=from_account,
                to=to,
                to_display=destination_label(view, step.type, to, equity),
                amount=cash,
                equity=equity,
            )
        )

    return ResolvedPlan(
        customer_id=view.customer_id,
        steps=tuple(resolved),
        balances_after={k: v.quantize(CENT, rounding=ROUND_DOWN) for k, v in balances.items()},
    )


def _amount(expr: AmountExpr, cash_by_step: dict[str, Decimal], balances: dict[str, Decimal],
            step_id: str) -> Decimal:
    """The cash an amount stands for when its step runs, given earlier steps' cash and the running balances."""
    if isinstance(expr, FixedAmount):
        return expr.value.quantize(CENT, rounding=ROUND_DOWN)
    if isinstance(expr, FractionOfBalance):
        if expr.account not in balances:
            raise EvaluationError("UNKNOWN_ACCOUNT", f"no account {expr.account!r}", step_id)
        # Round down so a fraction never exceeds what the customer said.
        value = (balances[expr.account] * expr.numerator / expr.denominator).quantize(CENT, rounding=ROUND_DOWN)
        if value <= 0:
            raise EvaluationError("ZERO_AMOUNT", "amount rounds to zero", step_id)
        return value
    if expr.step not in cash_by_step:
        raise EvaluationError(
            "FORWARD_REFERENCE", f"{expr.step} must be an earlier step", step_id
        )
    # Round down so a fraction never exceeds what the customer said.
    value = (cash_by_step[expr.step] * expr.numerator / expr.denominator).quantize(
        CENT, rounding=ROUND_DOWN
    )
    if value <= 0:
        raise EvaluationError("ZERO_AMOUNT", "amount rounds to zero", step_id)
    return value
