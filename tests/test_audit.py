"""The audit log: an intact chain verifies, and an edited or deleted entry is caught."""

import dataclasses
from datetime import UTC, datetime
from decimal import Decimal

from glassvault.audit import AuditLog
from glassvault.crypto import canonical_bytes

AT = datetime(2026, 10, 2, 9, 30, tzinfo=UTC)


def filled_log() -> AuditLog:
    log = AuditLog()
    for i in range(4):
        log.append("EVENT", {"i": i, "amount": Decimal("10.50")}, at=AT)
    return log


def test_intact_chain_verifies():
    assert filled_log().first_broken() is None


def test_edited_entry_is_detected():
    log = filled_log()
    log._entries[1] = dataclasses.replace(log._entries[1], data={"i": 1, "amount": "99999.00"})
    assert log.first_broken() == 1


def test_deleted_entry_is_detected():
    log = filled_log()
    del log._entries[2]
    assert log.first_broken() == 2


def test_canonical_json_is_order_independent():
    assert canonical_bytes({"b": 1, "a": Decimal("2.50")}) == canonical_bytes({"a": Decimal("2.50"), "b": 1})
