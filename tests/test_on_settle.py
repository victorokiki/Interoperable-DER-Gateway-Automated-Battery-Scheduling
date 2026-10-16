"""
tests/test_on_settle.py
SPC100644 Rev 05 6.2.4: after the BMS reports On, wait before any non-zero setpoint, so every string can
connect to the DC bus. Commanding power at once can leave strings out of the operation.

The driver holds a non-zero setpoint until the BMS has been On for bms_on_settle_sec (5 s by default).
The hold is a retry, never a rejection, and it never delays stopping (0 kW).
"""

import asyncio

import pytest

from capstone.modbus_driver import ON_SETTLE_SEC, STATE_ON
from fakes import FakeBMS, make_driver

SETTLE = 0.4          # seconds: short, so the tests are quick; the default is 5


def run(coro):
    return asyncio.run(coro)


def test_the_default_hold_is_the_five_seconds_the_spec_recommends():
    assert ON_SETTLE_SEC == 5.0


def test_a_non_zero_setpoint_is_held_until_the_bms_has_been_on_long_enough():
    async def go():
        fake = FakeBMS(bms_type=1)
        driver, _ = await make_driver(fake, bms_on_settle_sec=SETTLE)
        first = await driver.set_power(5.0, source="test")
        error = driver.last_dispatch_error
        wrote_early = fake.setpoint_history()
        await asyncio.sleep(SETTLE + 0.1)
        second = await driver.set_power(5.0, source="test")
        return first, error, wrote_early, second, fake.setpoint_kw()
    first, error, wrote_early, second, setpoint = run(go())
    assert first is False and error[0] == "settling" and "6.2.4" in error[1]
    assert not any(wrote_early)                      # nothing but zeros was ever written during the hold
    assert second is True and setpoint != 0


def test_the_hold_is_not_a_rejection_so_the_scheduler_retries_it():
    from capstone.scheduler import REFUSALS
    assert "settling" not in REFUSALS


def test_stopping_is_never_delayed():
    async def go():
        driver, _ = await make_driver(FakeBMS(bms_type=1), bms_on_settle_sec=60)
        return await driver.set_power(0, source="test"), await driver.safe_idle(source="test")
    assert run(go()) == (True, True)


def test_a_bms_that_is_not_on_is_left_to_the_existing_checks_not_the_hold():
    async def go():
        fake = FakeBMS(bms_type=1, operating_state=2)        # Standby, and it never accepts On
        fake.accept_on = False
        driver, _ = await make_driver(fake, bms_on_settle_sec=60)
        ok = await driver.set_power(5.0, source="test")
        return ok, driver.last_dispatch_error
    ok, error = run(go())
    assert ok is False and error[0] != "settling"


def test_leaving_on_and_coming_back_starts_the_wait_again():
    async def go():
        fake = FakeBMS(bms_type=1)
        driver, _ = await make_driver(fake, bms_on_settle_sec=SETTLE)
        await driver.set_power(5.0, source="test")             # starts the clock
        await asyncio.sleep(SETTLE + 0.1)
        fake.ir[1000] = 2                                       # drops to Standby
        await driver._read_state_and_mode()
        fake.ir[1000] = STATE_ON                                # and comes back On
        again = await driver.set_power(5.0, source="test")
        return again, driver.last_dispatch_error
    again, error = run(go())
    assert again is False and error[0] == "settling"


def test_a_reconnect_forgets_how_long_it_has_been_on():
    async def go():
        fake = FakeBMS(bms_type=1)
        driver, _ = await make_driver(fake, bms_on_settle_sec=SETTLE)
        await driver.set_power(5.0, source="test")
        await asyncio.sleep(SETTLE + 0.1)
        await driver.connect()                                  # link re-established
        return await driver.set_power(5.0, source="test"), driver.last_dispatch_error
    ok, error = run(go())
    assert ok is False and error[0] == "settling"


def test_zero_turns_the_hold_off():
    async def go():
        driver, _ = await make_driver(FakeBMS(bms_type=1), bms_on_settle_sec=0)
        return await driver.set_power(5.0, source="test")
    assert run(go()) is True


def test_the_hold_is_audited_as_not_written_with_the_reason(tmp_path):
    from capstone.audit_log import AuditLog
    from fakes import audit_rows

    async def go():
        audit = AuditLog(db_path=tmp_path / "audit.db")
        driver, _ = await make_driver(FakeBMS(bms_type=1), audit_log=audit, bms_on_settle_sec=60)
        await driver.set_power(5.0, source="test")
        audit.flush()
        return audit_rows(audit, "modbus_write")
    rows = run(go())
    assert any("not written" in str(r) and "6.2.4" in str(r) for r in rows), rows
