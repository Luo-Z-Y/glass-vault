"""Step-up: a second factor bound to one approval, required when the gateway's own risk rules say so."""

from decimal import Decimal

import pytest

from glassvault.audit import AuditLog
from glassvault.bank import seed_bank
from glassvault.crypto import DeviceKey, challenge_digest
from glassvault.evaluator import resolve
from glassvault.gateway import Gateway, GatewayError
from glassvault.schema import FixedAmount, PayNowTransfer, Plan
from glassvault.stepup import DigitalToken, StepUpVerifier


@pytest.fixture
def world(clock):
    bank = seed_bank(clock.now.date())
    token, step_up = DigitalToken(), StepUpVerifier()
    step_up.enrol(bank.customer_id, token)
    gateway = Gateway(bank, AuditLog(), step_up=step_up, clock=clock)
    phone = DeviceKey()
    gateway.register_device(bank.customer_id, phone.public_bytes)
    return bank, gateway, phone, token


def approve(world, clock, amount):
    bank, gateway, phone, _ = world
    plan = resolve(Plan(steps=[PayNowTransfer(id="t1", from_account="SAV", payee_id="P_MUM",
                                              amount=FixedAmount(value=Decimal(amount)))]), bank.view(), now=clock.now)
    challenge = gateway.issue_challenge(plan)
    return challenge, phone.sign(challenge_digest(challenge))


def code_of(fn):
    with pytest.raises(GatewayError) as e:
        fn()
    return e.value.code


def test_small_payment_needs_no_token(world, clock):
    challenge, signature = approve(world, clock, "300")
    assert not challenge.step_up_required
    world[1].execute(nonce=challenge.nonce, signature=signature)


def test_large_payment_needs_the_token_code_for_this_approval(world, clock):
    bank, gateway, _, token = world
    challenge, signature = approve(world, clock, "6000")  # >= S$5,000 and 12x Mum's usual
    assert challenge.step_up_required
    assert code_of(lambda: gateway.execute(nonce=challenge.nonce, signature=signature)) == "STEP_UP_REQUIRED"
    other, _ = approve(world, clock, "5500")
    assert code_of(lambda: gateway.execute(nonce=challenge.nonce, signature=signature,
                                           step_up_code=token.code(other.nonce))) == "STEP_UP_REQUIRED"
    gateway.execute(nonce=challenge.nonce, signature=signature, step_up_code=token.code(challenge.nonce))
    assert bank.accounts["SAV"].balance == Decimal("6400.00")


def test_three_wrong_codes_void_the_approval(world, clock):
    _, gateway, _, token = world
    challenge, signature = approve(world, clock, "6000")
    for _ in range(2):
        assert code_of(lambda: gateway.execute(nonce=challenge.nonce, signature=signature,
                                               step_up_code="000000")) == "STEP_UP_REQUIRED"
    assert code_of(lambda: gateway.execute(nonce=challenge.nonce, signature=signature,
                                           step_up_code="000000")) == "STEP_UP_LOCKED"
    assert code_of(lambda: gateway.execute(nonce=challenge.nonce, signature=signature,
                                           step_up_code=token.code(challenge.nonce))) == "NONCE_CONSUMED"


def test_step_up_flag_is_covered_by_the_signature(world, clock):
    challenge, _ = approve(world, clock, "6000")
    downgraded = challenge.model_copy(update={"step_up_required": False})
    assert challenge_digest(downgraded) != challenge_digest(challenge)
