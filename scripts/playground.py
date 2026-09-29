"""Interactive playground: try to rob the vault.

    uv run python -i scripts/playground.py

You get a bank, a gateway, the customer's phone key (`phone`) and a key the
rogue agent controls (`rogue`). A pending approval for S$500 to Mum is ready.
Goal: make money reach John Lim (payee "P_JLIM") without `phone.sign(...)`.
"""

from datetime import date
from decimal import Decimal

from glassvault.audit import AuditLog
from glassvault.bank import seed_bank
from glassvault.crypto import DeviceKey, challenge_digest
from glassvault.evaluator import resolve
from glassvault.gateway import Gateway, GatewayError
from glassvault.schema import BillPayment, EquityBuy, FixedAmount, FractionOfStep, OwnTransfer, PayNowTransfer, Plan

bank = seed_bank(date.today())
audit = AuditLog()
gateway = Gateway(bank, audit)
phone = DeviceKey()  # the customer's Face ID key
rogue = DeviceKey()  # a key the rogue agent controls
gateway.register_device(bank.customer_id, phone.public_bytes)


def draft(*steps) -> Plan:
    """A plan from steps: draft(pay("P_MUM", "500"), pay("P_JLIM", "9000", "t2"))."""
    return Plan(steps=list(steps))


def pay(payee_id: str, amount: str, step_id: str = "t1") -> PayNowTransfer:
    """A PayNow step from Savings: pay("P_JLIM", "900")."""
    return PayNowTransfer(id=step_id, from_account="SAV", payee_id=payee_id, amount=FixedAmount(value=Decimal(amount)))


def balances() -> dict[str, Decimal]:
    """Live balances, to check whether anything moved."""
    return {a.id: a.balance for a in bank.accounts.values()}


plan = resolve(draft(pay("P_MUM", "500")), bank.view(), now=gateway.now())
challenge = gateway.issue_challenge(plan)

print(__doc__)
print("Ready: plan (S$500 to Mum), challenge, gateway, phone, rogue, pay(), draft(), resolve(), balances()")
print("Honest path:  gateway.execute(nonce=challenge.nonce, signature=phone.sign(challenge_digest(challenge)))")
print("Audit trail:  [e.kind for e in audit.entries]")
