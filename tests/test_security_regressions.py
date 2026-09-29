"""Regression tests for the attacks found in the adversarial review of the week-1 core."""

import threading
from datetime import timedelta
from decimal import Decimal

import pytest

from glassvault.crypto import DeviceKey, challenge_digest
from glassvault.evaluator import resolve
from glassvault.gateway import GatewayError
from glassvault.schema import BillPayment, FixedAmount, PayNowTransfer, Plan


def approve(gateway, phone, plan):
    challenge = gateway.issue_challenge(plan)
    return challenge, phone.sign(challenge_digest(challenge))


def code_of(fn) -> str:
    with pytest.raises(GatewayError) as e:
        fn()
    return e.value.code


def pay_mum(bank, clock, amount="500"):
    plan = Plan(steps=[PayNowTransfer(id="t1", from_account="SAV", payee_id="P_MUM",
                                      amount=FixedAmount(value=Decimal(amount)))])
    return resolve(plan, bank.view(), now=clock.now)


# --- the customer must see exactly where the money goes ----------------------------------


def test_cannot_show_mum_and_pay_someone_else(bank, gateway, clock):
    honest = pay_mum(bank, clock)
    spoof = honest.model_copy(update={"steps": (honest.steps[0].model_copy(update={"to": "P_JLIM"}),)})
    assert spoof.steps[0].to_display == "Tan Mei Ling (+65 ••••1107)"
    assert code_of(lambda: gateway.issue_challenge(spoof)) == "STALE_PLAN"


def test_cannot_disguise_a_paynow_as_a_share_buy(bank, gateway, clock, brief_plan):
    share_step = resolve(brief_plan, bank.view(), now=clock.now).steps[1]
    honest = pay_mum(bank, clock, "481.44")
    spoof = honest.model_copy(update={"steps": (honest.steps[0].model_copy(update={"to_display": share_step.to_display}),)})
    assert code_of(lambda: gateway.issue_challenge(spoof)) == "STALE_PLAN"


def test_cannot_fill_a_different_stock_than_displayed(bank, gateway, clock, brief_plan):
    plan = resolve(brief_plan, bank.view(), now=clock.now)
    z74 = bank.market.quote("Z74")
    fill = plan.steps[1].equity.model_copy(update={"symbol": "Z74", "quote_id": z74.quote_id, "price": z74.price,
                                                   "quote_expires_at": z74.expires_at})
    spoof = plan.model_copy(update={"steps": (plan.steps[0], plan.steps[1].model_copy(update={"equity": fill}), plan.steps[2])})
    assert code_of(lambda: gateway.issue_challenge(spoof)) == "MALFORMED_PLAN"


def test_gateway_executes_its_own_copy_not_the_callers(bank, gateway, phone, clock):
    plan = pay_mum(bank, clock)
    challenge, signature = approve(gateway, phone, plan)
    assert gateway.pending_plan(challenge.nonce) == plan
    gateway.execute(nonce=challenge.nonce, signature=signature)  # no plan object needed
    assert bank.accounts["SAV"].balance == Decimal("11900.00")


# --- enrolment is write-once ---------------------------------------------------------------


def test_agent_cannot_enrol_its_own_key(bank, gateway, phone, clock):
    rogue = DeviceKey()
    assert code_of(lambda: gateway.register_device(bank.customer_id, rogue.public_bytes)) == "DEVICE_ALREADY_REGISTERED"
    plan = pay_mum(bank, clock)
    challenge = gateway.issue_challenge(plan)
    assert code_of(lambda: gateway.execute(plan, nonce=challenge.nonce,
                                           signature=rogue.sign(challenge_digest(challenge)))) == "SIGNATURE_INVALID"
    # The genuine phone still works.
    gateway.execute(plan, nonce=challenge.nonce, signature=phone.sign(challenge_digest(challenge)))


# --- malformed plans never reach the customer ---------------------------------------------


@pytest.mark.parametrize("amount", ["499.995", "NaN", "-5.00", "0"])
def test_malformed_amounts_are_refused_before_signing(bank, gateway, clock, amount):
    honest = pay_mum(bank, clock)
    bad = honest.model_copy(update={"steps": (honest.steps[0].model_copy(update={"amount": Decimal(amount)}),)})
    assert code_of(lambda: gateway.issue_challenge(bad)) == "MALFORMED_PLAN"


def test_unencodable_text_is_refused_and_audited(bank, gateway, audit, clock):
    honest = pay_mum(bank, clock)
    bad = honest.model_copy(update={"steps": (honest.steps[0].model_copy(update={"to_display": "\ud800"}),)})
    assert code_of(lambda: gateway.issue_challenge(bad)) == "MALFORMED_PLAN"
    assert audit.entries[-1].kind == "CHALLENGE_REFUSED"


def test_unaffordable_plan_is_refused_before_signing(bank, gateway, clock):
    plan = pay_mum(bank, clock, "9000")
    bank.accounts["SAV"].balance = Decimal("100.00")
    assert code_of(lambda: gateway.issue_challenge(plan)) == "INSUFFICIENT_FUNDS"


# --- time comes from the gateway -----------------------------------------------------------


def test_caller_cannot_mint_a_long_lived_quote(bank, gateway, phone, clock, brief_plan):
    far_future = clock.now + timedelta(days=365)
    plan = resolve(brief_plan, bank.view(), now=far_future)  # ignored: the market uses the gateway clock
    assert plan.steps[1].equity.quote_expires_at == clock.now + timedelta(seconds=60)
    challenge, signature = approve(gateway, phone, plan)
    clock.advance(seconds=90)
    assert code_of(lambda: gateway.execute(plan, nonce=challenge.nonce, signature=signature)) == "QUOTE_EXPIRED"


# --- concurrency and ordering --------------------------------------------------------------


def test_concurrent_approvals_cannot_double_spend(bank, gateway, phone, clock):
    plan = Plan(steps=[PayNowTransfer(id="t1", from_account="JNT", payee_id="P_MUM",
                                      amount=FixedAmount(value=Decimal("3000")))])
    approvals = [approve(gateway, phone, resolve(plan, bank.view(), now=clock.now)) for _ in range(8)]
    results: list[str] = []

    def run(challenge, signature):
        try:
            gateway.execute(nonce=challenge.nonce, signature=signature)
            results.append("ok")
        except GatewayError as e:
            results.append(e.code)

    threads = [threading.Thread(target=run, args=a) for a in approvals]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1
    assert bank.accounts["JNT"].balance == Decimal("200.00")


def test_audit_failure_moves_no_money(bank, gateway, phone, clock, monkeypatch):
    plan = pay_mum(bank, clock)
    challenge, signature = approve(gateway, phone, plan)
    original = gateway.audit.append

    def failing_append(kind, data, *, at):
        if kind == "EXECUTED":
            raise OSError("disk full")
        return original(kind, data, at=at)

    monkeypatch.setattr(gateway.audit, "append", failing_append)
    assert code_of(lambda: gateway.execute(plan, nonce=challenge.nonce, signature=signature)) == "INTERNAL_ERROR"
    assert bank.accounts["SAV"].balance == Decimal("12400.00")


def test_failed_execution_still_uses_up_the_approval(bank, gateway, phone, clock, brief_plan):
    plan = resolve(brief_plan, bank.view(), now=clock.now)
    challenge, signature = approve(gateway, phone, plan)
    clock.advance(seconds=90)
    assert code_of(lambda: gateway.execute(plan, nonce=challenge.nonce, signature=signature)) == "QUOTE_EXPIRED"
    assert code_of(lambda: gateway.execute(plan, nonce=challenge.nonce, signature=signature)) == "NONCE_CONSUMED"


def test_signature_for_one_challenge_does_not_approve_another(bank, gateway, phone, clock):
    plan = pay_mum(bank, clock)
    first, signature = approve(gateway, phone, plan)
    second = gateway.issue_challenge(plan)
    assert code_of(lambda: gateway.execute(plan, nonce=second.nonce, signature=signature)) == "SIGNATURE_INVALID"


def test_bad_signature_does_not_burn_the_approval(bank, gateway, phone, clock):
    plan = pay_mum(bank, clock)
    challenge, signature = approve(gateway, phone, plan)
    assert code_of(lambda: gateway.execute(plan, nonce=challenge.nonce, signature=b"\x00" * 64)) == "SIGNATURE_INVALID"
    gateway.execute(plan, nonce=challenge.nonce, signature=signature)


def test_unrelated_credit_does_not_invalidate_an_approval(bank, gateway, phone, clock):
    plan = resolve(Plan(steps=[BillPayment(id="t1", from_account="SAV", biller_id="SINGTEL",
                                           amount=FixedAmount(value=Decimal("58.90")))]), bank.view(), now=clock.now)
    challenge, signature = approve(gateway, phone, plan)
    bank.accounts["JNT"].balance += Decimal("250.00")  # salary lands in another account meanwhile
    gateway.execute(plan, nonce=challenge.nonce, signature=signature)
    assert bank.accounts["SAV"].balance == Decimal("12341.10")
