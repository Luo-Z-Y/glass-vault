from decimal import Decimal

import pytest

from glassvault.crypto import DeviceKey, challenge_digest
from glassvault.evaluator import resolve
from glassvault.gateway import GatewayError
from glassvault.schema import FixedAmount, PayNowTransfer, Plan, ResolvedStep

THEFT = ResolvedStep(id="t9", type="paynow", from_account="SAV", to="P_JLIM",
                     to_display="John Lim Kah Seng", amount=Decimal("9000.00"))


def approve(gateway, phone, plan):
    challenge = gateway.issue_challenge(plan)
    return challenge, phone.sign(challenge_digest(challenge))


def code_of(fn) -> str:
    with pytest.raises(GatewayError) as e:
        fn()
    return e.value.code


def test_signed_plan_executes_and_is_audited(bank, gateway, audit, phone, clock, brief_plan):
    plan = resolve(brief_plan, bank.view(), now=clock.now)
    challenge, signature = approve(gateway, phone, plan)

    receipts = gateway.execute(plan, nonce=challenge.nonce, signature=signature)

    assert [r.step_id for r in receipts] == ["t1", "t2", "t3"]
    assert bank.accounts["SAV"].balance == Decimal("11257.65")
    assert bank.accounts["JNT"].balance == Decimal("3718.56")
    assert audit.entries[-1].kind == "EXECUTED" and audit.first_broken() is None


def test_no_approval(bank, gateway, clock, brief_plan):
    plan = resolve(brief_plan, bank.view(), now=clock.now)
    assert code_of(lambda: gateway.execute(plan)) == "SIGNATURE_REQUIRED"


def test_plan_changed_after_signing(bank, gateway, phone, clock, brief_plan):
    plan = resolve(brief_plan, bank.view(), now=clock.now)
    challenge, signature = approve(gateway, phone, plan)
    tampered = plan.model_copy(update={"steps": plan.steps + (THEFT,)})
    assert code_of(lambda: gateway.execute(tampered, nonce=challenge.nonce, signature=signature)) == "PAYLOAD_HASH_MISMATCH"
    # The genuine plan can still go through with the genuine approval.
    gateway.execute(plan, nonce=challenge.nonce, signature=signature)


def test_one_cent_change_is_caught(bank, gateway, phone, clock, brief_plan):
    plan = resolve(brief_plan, bank.view(), now=clock.now)
    challenge, signature = approve(gateway, phone, plan)
    first = plan.steps[0].model_copy(update={"amount": plan.steps[0].amount + Decimal("0.01")})
    tampered = plan.model_copy(update={"steps": (first,) + plan.steps[1:]})
    assert code_of(lambda: gateway.execute(tampered, nonce=challenge.nonce, signature=signature)) == "PAYLOAD_HASH_MISMATCH"


def test_replay(bank, gateway, phone, clock, brief_plan):
    plan = resolve(brief_plan, bank.view(), now=clock.now)
    challenge, signature = approve(gateway, phone, plan)
    gateway.execute(plan, nonce=challenge.nonce, signature=signature)
    assert code_of(lambda: gateway.execute(plan, nonce=challenge.nonce, signature=signature)) == "NONCE_CONSUMED"


def test_forged_signature(bank, gateway, clock, brief_plan):
    plan = resolve(brief_plan, bank.view(), now=clock.now)
    challenge = gateway.issue_challenge(plan)
    forged = DeviceKey().sign(challenge_digest(challenge))
    assert code_of(lambda: gateway.execute(plan, nonce=challenge.nonce, signature=forged)) == "SIGNATURE_INVALID"


def test_expired_approval(bank, gateway, phone, clock, brief_plan):
    plan = resolve(brief_plan, bank.view(), now=clock.now)
    challenge, signature = approve(gateway, phone, plan)
    clock.advance(minutes=3)
    assert code_of(lambda: gateway.execute(plan, nonce=challenge.nonce, signature=signature)) == "CHALLENGE_EXPIRED"


def test_expired_share_quote(bank, gateway, phone, clock, brief_plan):
    plan = resolve(brief_plan, bank.view(), now=clock.now)
    challenge, signature = approve(gateway, phone, plan)
    clock.advance(seconds=90)  # approval still valid (2 min) but the 60 s quote is not
    assert code_of(lambda: gateway.execute(plan, nonce=challenge.nonce, signature=signature)) == "QUOTE_EXPIRED"
    assert bank.accounts["SAV"].balance == Decimal("12400.00")


def test_balances_moved_since_approval_is_all_or_nothing(bank, gateway, phone, clock):
    pay_mum = Plan(steps=[PayNowTransfer(id="t1", from_account="JNT", payee_id="P_MUM",
                                         amount=FixedAmount(value=Decimal("3000")))])
    first = resolve(pay_mum, bank.view(), now=clock.now)
    second = resolve(pay_mum, bank.view(), now=clock.now)  # drafted from the same snapshot
    c1, s1 = approve(gateway, phone, first)
    c2, s2 = approve(gateway, phone, second)
    gateway.execute(first, nonce=c1.nonce, signature=s1)
    assert code_of(lambda: gateway.execute(second, nonce=c2.nonce, signature=s2)) in {"INSUFFICIENT_FUNDS", "STALE_PLAN"}
    assert bank.accounts["JNT"].balance == Decimal("200.00")


def test_every_rejection_is_logged(bank, gateway, audit, clock, brief_plan):
    plan = resolve(brief_plan, bank.view(), now=clock.now)
    for _ in range(3):
        with pytest.raises(GatewayError):
            gateway.execute(plan)
    assert sum(e.kind == "REJECTED" for e in audit.entries) == 3
