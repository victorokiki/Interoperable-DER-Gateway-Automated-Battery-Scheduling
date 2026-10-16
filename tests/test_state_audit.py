"""
tests/test_state_audit.py
NF-03: every BMS state and health change reaches the audit log when the status poll sees it.

Found by the load and audit check of the first real-battery test: a Shutting Down, a Standby and a return to On
during manual control and normal running left no audit row, because only the code paths that happened to read
31000 (a command, a write's verification) reported it.
"""

import asyncio

from capstone.audit_log import AuditLog
from fakes import FakeBMS, audit_rows, make_driver


def run(coro):
    return asyncio.run(coro)


def details(audit):
    audit.flush()
    return [r["detail"] for r in audit_rows(audit, "bms_condition")]


def test_the_status_poll_audits_a_state_change_it_alone_sees(tmp_path):
    async def go():
        audit = AuditLog(db_path=tmp_path / "audit.db")
        fake = FakeBMS(bms_type=1)
        driver, _ = await make_driver(fake, audit_log=audit)
        fake.user_controlled(True)                  # an operator has control: the gateway commands nothing
        await driver.supervise()
        fake.ir[1000] = 5                           # Shutting Down, seen by the poll only
        await driver.supervise()
        fake.ir[1000] = 2                           # Standby
        await driver.supervise()
        return details(audit)
    rows = run(go())
    assert "BMS operating state Shutting Down" in rows and "BMS operating state Standby" in rows


def test_a_health_change_is_audited_too(tmp_path):
    async def go():
        audit = AuditLog(db_path=tmp_path / "audit.db")
        fake = FakeBMS(bms_type=1)
        driver, _ = await make_driver(fake, audit_log=audit)
        await driver.supervise()
        fake.ir[1001] = 2                           # Warning
        await driver.supervise()
        fake.ir[1001] = 3                           # Fault
        await driver.supervise()
        return details(audit)
    rows = run(go())
    assert "BMS operating status Warning" in rows and "BMS operating status Fault" in rows


def test_an_unchanged_state_is_not_audited_again(tmp_path):
    async def go():
        audit = AuditLog(db_path=tmp_path / "audit.db")
        driver, _ = await make_driver(FakeBMS(bms_type=1), audit_log=audit)
        for _ in range(4):
            await driver.supervise()
        return details(audit)
    rows = run(go())
    assert sum(1 for d in rows if d == "BMS operating state On") <= 1
    assert sum(1 for d in rows if d.startswith("BMS operating status")) == 1
