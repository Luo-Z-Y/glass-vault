"""Read-only enquiries: balances and recent transactions, answered from bank records.

The drafter only chooses which question the customer asked (an `Enquiry`). Every
number and line here comes from the bank's records, in fixed wording, so an LLM
can never misstate a balance. Statement descriptions can be written by third
parties (a sender's PayNow reference), so they never reach an LLM, and any that
read like instructions are withheld from the screen and the voice as well.
Nothing here can move money: answers need no signature and never touch the ledger.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from .bank import BankView, Transaction
from .context import sanitize_label, scan_text
from .renderer import spell_amount
from .schema import Enquiry, EnquiryAnswer

if TYPE_CHECKING:  # the answerer only reads executions
    from .gateway import Execution

WITHHELD = "[description withheld: it contains instructions]"
WITHHELD_SPOKEN = "a transaction whose description I can't read out"


def answer(enquiry: Enquiry, view: BankView, history: Sequence[Execution]) -> EnquiryAnswer:
    if enquiry.kind == "balance":
        return _balances(enquiry, view)
    return _transactions(enquiry, view, history)


def statement(view: BankView, history: Sequence[Execution]) -> list[Transaction]:
    """Seeded statement lines plus everything executed through the gateway, newest first."""
    executed = [_from_execution(e, view) for e in reversed(history)]
    return sorted([*executed, *view.transactions], key=lambda t: t.on, reverse=True)


def _balances(enquiry: Enquiry, view: BankView) -> EnquiryAnswer:
    if enquiry.account in view.accounts:
        accounts = [view.accounts[enquiry.account]]
    else:
        accounts = list(view.accounts.values())
    lines = tuple(f"{a.name}: S${a.balance:,.2f}" for a in accounts)
    spoken = " ".join(f"Your {a.name} has {spell_amount(a.balance)}." for a in accounts)
    return EnquiryAnswer(lines=lines, spoken_script=spoken)


def _transactions(enquiry: Enquiry, view: BankView, history: Sequence[Execution]) -> EnquiryAnswer:
    rows = [
        t for t in statement(view, history)
        if (enquiry.account is None or t.account == enquiry.account)
        and (enquiry.payee_id is None or t.payee_id == enquiry.payee_id)
    ][: enquiry.limit]
    if not rows:
        payee = view.payees.get(enquiry.payee_id or "")
        text = f"No recent payments to {payee.legal_name}." if payee else "No recent transactions."
        return EnquiryAnswer(lines=(text,), spoken_script=text)
    lines = tuple(
        f"{t.on:%d %b}  {_safe(t.description)}  {'+' if t.amount > 0 else '-'}S${abs(t.amount):,.2f}" for t in rows
    )
    spoken = f"Your last {len(rows)} transaction{'s' if len(rows) != 1 else ''}. " + " ".join(
        f"{t.on:%d %B}: {_safe(t.description, spoken=True)}, {spell_amount(abs(t.amount))} "
        f"{'in' if t.amount > 0 else 'out'}." for t in rows
    )
    return EnquiryAnswer(lines=lines, spoken_script=spoken)


def _safe(description: str, *, spoken: bool = False) -> str:
    if scan_text(description, data_field=True):
        return WITHHELD_SPOKEN if spoken else WITHHELD
    return sanitize_label(description, max_len=60)


def _from_execution(execution: Execution, view: BankView) -> Transaction:
    step = execution.step
    match step.type:
        case "own_transfer":
            description = f"Transfer to {step.to_display}"
        case "paynow":
            payee = view.payees.get(step.to)
            description = f"PayNow to {payee.legal_name if payee else step.to_display}"
        case "bill_payment":
            description = f"Bill payment {step.to_display}"
        case _:
            description = f"Buy {step.to_display}"
    return Transaction(
        on=execution.at.date(),
        account=step.from_account,
        description=description,
        amount=-step.amount,
        payee_id=step.to if step.type == "paynow" else None,
    )
