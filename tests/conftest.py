"""Shared fixtures: the seeded demo bank, a controllable clock, and a gateway with the customer's device enrolled."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from glassvault.audit import AuditLog
from glassvault.bank import Bank, seed_bank
from glassvault.crypto import DeviceKey
from glassvault.gateway import Gateway
from glassvault.schema import BillPayment, EquityBuy, FixedAmount, FractionOfStep, OwnTransfer, Plan


class FakeClock:
    """A clock that only moves when the test calls `advance`, so expiry tests need no sleeping."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    """Starts at a fixed moment, so the seed bank's dates and every expiry are the same on each run."""
    return FakeClock(datetime(2026, 10, 2, 9, 30, tzinfo=UTC))


@pytest.fixture
def bank(clock: FakeClock) -> Bank:
    """The demo customer from `seed_bank`, dated to the clock's day."""
    return seed_bank(clock.now.date())


@pytest.fixture
def audit() -> AuditLog:
    return AuditLog()


@pytest.fixture
def phone() -> DeviceKey:
    """The customer's device key, standing in for their passkey."""
    return DeviceKey()


@pytest.fixture
def gateway(bank: Bank, audit: AuditLog, clock: FakeClock, phone: DeviceKey) -> Gateway:
    """A gateway over `bank` with `phone` enrolled: risk rules on, no step-up token."""
    gw = Gateway(bank, audit, clock=clock)
    gw.register_device(bank.customer_id, phone.public_bytes)
    return gw


@pytest.fixture
def brief_plan() -> Plan:
    """The brief's own example: move a grand to joint, buy DBS shares with half."""
    return Plan(steps=[
        OwnTransfer(id="t1", from_account="SAV", to_account="JNT", amount=FixedAmount(value=Decimal("1000"))),
        EquityBuy(id="t2", funding_account="JNT", symbol="D05",
                  notional=FractionOfStep(step="t1", numerator=1, denominator=2)),
        BillPayment(id="t3", from_account="SAV", biller_id="SP_GROUP", amount=FixedAmount(value=Decimal("142.35"))),
    ])
