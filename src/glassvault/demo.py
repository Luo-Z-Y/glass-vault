"""CLI demo: one signed happy path, then a rogue agent tries six attacks on the gateway.

The plan below is written by hand, so this runs without an LLM; the web demo
(``python -m glassvault.web.server``) runs the full pipeline.
Run with: uv run python -m glassvault.demo
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from .audit import AuditLog
from .bank import seed_bank
from .crypto import DeviceKey, challenge_digest
from .evaluator import resolve
from .gateway import Gateway, GatewayError
from .schema import BillPayment, EquityBuy, FixedAmount, FractionOfStep, OwnTransfer, PayNowTransfer, Plan

UTTERANCE = "Move a grand to my joint account, buy DBS shares with half of it, and pay my SP bill."


def main() -> None:
    """Enrol the customer's device, execute the brief's example, then try each attack and report."""
    now = datetime.now(UTC)
    bank = seed_bank(now.date())
    audit = AuditLog()
    gateway = Gateway(bank, audit)
    phone = DeviceKey()  # the customer's Face ID passkey
    gateway.register_device(bank.customer_id, phone.public_bytes)

    print(f'Customer says: "{UTTERANCE}"\n')
    draft = Plan(steps=[
        OwnTransfer(id="t1", from_account="SAV", to_account="JNT",
                    amount=FixedAmount(value=Decimal("1000")),
                    evidence={"amount": "a grand", "to_account": "my joint account"}),
        EquityBuy(id="t2", funding_account="JNT", symbol="D05",
                  notional=FractionOfStep(step="t1", numerator=1, denominator=2),
                  evidence={"symbol": "DBS shares", "notional": "half of it"}),
        BillPayment(id="t3", from_account="SAV", biller_id="SP_GROUP",
                    amount=FixedAmount(value=Decimal("142.35")),
                    evidence={"biller_id": "my SP bill"}),
    ])
    plan = resolve(draft, bank.view(), now=now)

    print("Confirmation screen (rendered from bank records, not the LLM):")
    for s in plan.steps:
        print(f"  {s.id}  {s.from_account} -> {s.to_display:<48} S${s.amount:>9,.2f}")
    print(f"  Balances after: {', '.join(f'{k} S${v:,.2f}' for k, v in plan.balances_after.items())}\n")

    challenge = gateway.issue_challenge(plan)
    signature = phone.sign(challenge_digest(challenge))  # customer approves with Face ID
    receipts = gateway.execute(plan, nonce=challenge.nonce, signature=signature)
    print("Approved with Face ID. Executed:")
    for r in receipts:
        print(f"  {r.reference}  {r.summary}")

    print("\nROGUE AGENT MODE: the agent now tries to move money on its own.\n")
    attacker = DeviceKey()
    theft = resolve(Plan(steps=[PayNowTransfer(id="t1", from_account="SAV", payee_id="P_JLIM",
                                               amount=FixedAmount(value=Decimal("9000")))]),
                    bank.view(), now=now)
    blocked = 0

    def attempt(label: str, fn) -> None:
        """Run one attack and print whether the gateway blocked it."""
        nonlocal blocked
        try:
            fn()
            print(f"  {label:<56} !! EXECUTED")
        except GatewayError as e:
            blocked += 1
            print(f"  {label:<56} blocked: {e.status} {e.code}")

    # 1. Skip the customer entirely.
    attempt("1. Send S$9,000 with no approval", lambda: gateway.execute(theft))

    # 2. Get a genuine approval, then swap in a different plan before executing.
    honest = resolve(Plan(steps=[BillPayment(id="t1", from_account="SAV", biller_id="SINGTEL",
                                             amount=FixedAmount(value=Decimal("58.90")))]),
                     bank.view(), now=now)
    c2 = gateway.issue_challenge(honest)
    s2 = phone.sign(challenge_digest(c2))
    tampered = honest.model_copy(update={"steps": honest.steps + (theft.steps[0].model_copy(update={"id": "t2"}),)})
    attempt("2. Add a S$9,000 step after the customer signed", lambda: gateway.execute(
        tampered, nonce=c2.nonce, signature=s2))

    # 3. Replay the customer's earlier approval.
    attempt("3. Replay an approval that was already used", lambda: gateway.execute(
        plan, nonce=challenge.nonce, signature=signature))

    # 4. A perfectly formed plan, within every risk limit, still needs the customer's key.
    small = resolve(Plan(steps=[PayNowTransfer(id="t1", from_account="SAV", payee_id="P_JLIM",
                                               amount=FixedAmount(value=Decimal("900")))]),
                    bank.view(), now=now)

    def forge() -> None:
        c4 = gateway.issue_challenge(small)
        gateway.execute(small, nonce=c4.nonce, signature=attacker.sign(challenge_digest(c4)))
    attempt("4. Sign a valid-looking plan with the agent's own key", forge)

    # 5. Show the customer "Mum" on screen while paying someone else.
    to_mum = resolve(Plan(steps=[PayNowTransfer(id="t1", from_account="SAV", payee_id="P_MUM",
                                                amount=FixedAmount(value=Decimal("500")))]),
                     bank.view(), now=now)
    spoof = to_mum.model_copy(update={"steps": (to_mum.steps[0].model_copy(update={"to": "P_JLIM"}),)})
    attempt("5. Display 'Tan Mei Ling' but pay John Lim", lambda: gateway.issue_challenge(spoof))

    # 6. Enrol the agent's own key as the customer's device.
    attempt("6. Replace the customer's Face ID key with its own", lambda: gateway.register_device(
        bank.customer_id, attacker.public_bytes))

    broken = audit.first_broken()
    print(f"\nAttacks blocked: {blocked}/6   Funds moved by the agent without approval: S$0.00")
    print(f"Audit log: {len(audit.entries)} entries, hash chain {'intact' if broken is None else f'BROKEN at {broken}'}")


if __name__ == "__main__":
    main()
