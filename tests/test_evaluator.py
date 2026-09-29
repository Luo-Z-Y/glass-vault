from decimal import Decimal

import pytest
from pydantic import ValidationError

from glassvault.evaluator import EvaluationError, resolve
from glassvault.schema import EquityBuy, FixedAmount, FractionOfStep, OwnTransfer, PayNowTransfer, Plan


def test_brief_example_resolves_in_order(bank, clock, brief_plan):
    plan = resolve(brief_plan, bank.view(), now=clock.now)
    t1, t2, t3 = plan.steps

    assert t1.amount == Decimal("1000.00") and t1.to_display == "Joint Account"
    # Half of t1 is S$500 notional; 12 DBS shares at S$40.12 cost S$481.44, the rest stays in Joint.
    assert t2.equity.quantity == 12 and t2.amount == Decimal("481.44")
    assert t3.to_display == "SP Group"  # the biller's poisoned reference text is never shown
    assert plan.balances_after == {"SAV": Decimal("11257.65"), "JNT": Decimal("3718.56")}


def test_does_not_touch_the_real_bank(bank, clock, brief_plan):
    resolve(brief_plan, bank.view(), now=clock.now)
    assert bank.accounts["SAV"].balance == Decimal("12400.00")


def test_later_step_sees_earlier_step_balance(bank, clock):
    # Joint has S$3,200 and cannot fund S$4,000 unless t1 tops it up first.
    plan = Plan(steps=[
        OwnTransfer(id="t1", from_account="SAV", to_account="JNT", amount=FixedAmount(value=Decimal("1000"))),
        PayNowTransfer(id="t2", from_account="JNT", payee_id="P_MUM", amount=FixedAmount(value=Decimal("4000"))),
    ])
    assert resolve(plan, bank.view(), now=clock.now).balances_after["JNT"] == Decimal("200.00")


def test_insufficient_funds_names_the_step(bank, clock):
    plan = Plan(steps=[
        PayNowTransfer(id="t1", from_account="JNT", payee_id="P_MUM", amount=FixedAmount(value=Decimal("5000"))),
    ])
    with pytest.raises(EvaluationError) as e:
        resolve(plan, bank.view(), now=clock.now)
    assert e.value.code == "INSUFFICIENT_FUNDS" and e.value.step_id == "t1"


@pytest.mark.parametrize("ref", ["t2", "t1"])
def test_fraction_must_reference_an_earlier_step(bank, clock, ref):
    plan = Plan(steps=[
        EquityBuy(id="t1", funding_account="SAV", symbol="D05",
                  notional=FractionOfStep(step=ref, numerator=1, denominator=2)),
        OwnTransfer(id="t2", from_account="SAV", to_account="JNT", amount=FixedAmount(value=Decimal("1000"))),
    ])
    with pytest.raises(EvaluationError) as e:
        resolve(plan, bank.view(), now=clock.now)
    assert e.value.code == "FORWARD_REFERENCE"


@pytest.mark.parametrize(
    "step, code",
    [
        (PayNowTransfer(id="t1", from_account="SAV", payee_id="P_ATTACKER", amount=FixedAmount(value=Decimal("9000"))),
         "UNKNOWN_PAYEE"),
        (OwnTransfer(id="t1", from_account="SAV", to_account="SAV", amount=FixedAmount(value=Decimal("10"))),
         "SAME_ACCOUNT"),
        (EquityBuy(id="t1", funding_account="SAV", symbol="TSLA", notional=FixedAmount(value=Decimal("100"))),
         "UNKNOWN_SYMBOL"),
        (EquityBuy(id="t1", funding_account="SAV", symbol="D05", notional=FixedAmount(value=Decimal("20"))),
         "NOTIONAL_TOO_SMALL"),
    ],
)
def test_rejects_bad_steps(bank, clock, step, code):
    with pytest.raises(EvaluationError) as e:
        resolve(Plan(steps=[step]), bank.view(), now=clock.now)
    assert e.value.code == code


def test_schema_rejects_malformed_drafts():
    with pytest.raises(ValidationError):  # more than the whole step
        FractionOfStep(step="t1", numerator=3, denominator=2)
    with pytest.raises(ValidationError):  # negative money
        FixedAmount(value=Decimal("-5"))
    with pytest.raises(ValidationError):  # sub-cent precision
        FixedAmount(value=Decimal("1.005"))
    with pytest.raises(ValidationError):  # duplicate step ids
        Plan(steps=[
            OwnTransfer(id="t1", from_account="SAV", to_account="JNT", amount=FixedAmount(value=Decimal("1"))),
            OwnTransfer(id="t1", from_account="SAV", to_account="JNT", amount=FixedAmount(value=Decimal("2"))),
        ])
