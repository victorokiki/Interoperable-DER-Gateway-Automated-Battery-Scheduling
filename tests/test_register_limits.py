"""
tests/test_register_limits.py
The BMS's load rules (SPC100644 Rev 03 §4.3, §5): at most 25 registers per
read or write request, and at most 50 register accesses a second per
server, counted over all clients. Requests above the rate are dropped, not
queued, and the BMS says so by no answer at all.

FakeBMS applies both rules and logs every request, so a test can check that
the real ModbusDriver stays inside them, and the driver's own counter
(register_meter) is checked against what the BMS saw.
"""

import asyncio
from types import SimpleNamespace

import pytest

from capstone.audit_log import AuditLog
from capstone.modbus_driver import MAX_REGISTER_RATE, MAX_REGISTERS_PER_REQUEST
from fakes import FakeBMS, audit_rows, make_driver


def test_the_fake_bms_answers_exception_03_above_25_registers():
    async def run():
        fake = FakeBMS()
        return (await fake.read_input_registers(1000, count=25),
                await fake.read_input_registers(1000, count=26),
                await fake.read_holding_registers(1000, count=26),
                await fake.write_registers(1100, [0] * 25),
                await fake.write_registers(1100, [0] * 26))
    ok_read, big_read, big_holding, ok_write, big_write = asyncio.run(run())
    assert not ok_read.isError() and not ok_write.isError()
    assert [big_read.exception_code, big_holding.exception_code, big_write.exception_code] == [3, 3, 3]


def test_peak_register_rate_is_the_busiest_second():
    fake = FakeBMS()
    # 20 registers at t=0, 25 at 0.5 s, 10 at 0.9 s: all three fall in one second (55). The
    # 4th, at 1.2 s, no longer shares a second with the first, so it never makes a busier one.
    fake.access_log = [(0.0, 20), (0.5, 25), (0.9, 10), (1.2, 5)]
    assert fake.peak_register_rate() == 55
    fake.access_log = [(0.0, 20), (1.0, 25), (2.0, 10)]            # a full second apart: never together
    assert fake.peak_register_rate() == 25
    assert fake.largest_request() == 25


def test_the_driver_never_sends_a_request_over_25_registers():
    async def run():
        driver, _ = await make_driver()
        fake = driver._client
        before = len(fake.access_log)
        errors = []
        for call in (lambda: driver._client.read_input_registers(1000, count=MAX_REGISTERS_PER_REQUEST + 1, slave=255),
                     lambda: driver._client.read_holding_registers(1000, count=MAX_REGISTERS_PER_REQUEST + 1, slave=255),
                     lambda: driver._client.write_registers(1100, [0] * (MAX_REGISTERS_PER_REQUEST + 1), slave=255)):
            with pytest.raises(ValueError, match="25 per request"):
                await call()
            errors.append(True)
        return fake, before, errors
    fake, before, errors = asyncio.run(run())
    assert len(errors) == 3
    assert len(fake.access_log) == before                          # nothing was sent


def _peak(log):
    return FakeBMS.peak_register_rate(SimpleNamespace(access_log=log))


def test_the_worst_realistic_second_stays_inside_both_limits():
    """The loops run a heartbeat every second and a status poll every ten; a
    schedule change adds a setpoint write and its read-back. The busiest
    second there can be has all three at once."""
    async def run():
        driver, _ = await make_driver()
        fake = driver._client
        await asyncio.sleep(1.1)                       # the start-up burst leaves the window
        mark = len(fake.access_log)
        await driver.heartbeat_once()
        await driver.supervise()                       # status + limits + grid modes
        assert await driver.set_power(50)              # setpoint write + read-back
        return driver, fake, mark
    driver, fake, mark = asyncio.run(run())
    seen = sum(n for _, n in fake.access_log)
    assert driver.register_meter.total == seen                     # the driver's count is the BMS's
    assert driver.register_meter.largest_request == fake.largest_request() <= MAX_REGISTERS_PER_REQUEST
    assert 0 < _peak(fake.access_log[mark:]) <= MAX_REGISTER_RATE


def test_start_up_taking_over_and_the_first_setpoint_stay_inside_the_rate_limit():
    async def run():
        driver, _ = await make_driver()                # connect: type probe, state, modes, On
        await driver.heartbeat_once()
        assert await driver.set_power(50)              # the first dispatch
        return driver, driver._client
    driver, fake = asyncio.run(run())
    assert _peak(fake.access_log) <= MAX_REGISTER_RATE
    assert driver.register_meter.peak_rate <= MAX_REGISTER_RATE


def test_going_over_50_a_second_is_audited_once_and_so_is_the_recovery(tmp_path):
    async def run():
        audit = AuditLog(db_path=tmp_path / "audit.db")
        driver, _ = await make_driver(audit_log=audit)
        quiet = len(audit_rows(audit, "bms_condition"))
        for _ in range(30):                                        # 60 registers, back to back
            await driver._client.read_input_registers(1000, count=2, slave=255)
        over = [r for r in audit_rows(audit, "bms_condition") if r["outcome"] == "over_limit"]
        await asyncio.sleep(1.1)                                   # the window empties
        await driver._client.read_input_registers(1000, count=1, slave=255)
        await driver._client.read_input_registers(1000, count=1, slave=255)
        rows = audit_rows(audit, "bms_condition")
        return quiet, over, rows, driver
    quiet, over, rows, driver = asyncio.run(run())
    assert len(over) == 1 and "50 a second" in over[0]["detail"]   # once, not once per request
    assert [r["outcome"] for r in rows[quiet:]] == ["over_limit", "ok"]
    assert driver.register_meter.peak_rate > MAX_REGISTER_RATE


def test_a_normal_start_up_writes_no_over_limit_row(tmp_path):
    async def run():
        audit = AuditLog(db_path=tmp_path / "audit.db")
        driver, _ = await make_driver(audit_log=audit)
        await driver.supervise()
        return audit_rows(audit, "bms_condition")
    assert [r for r in asyncio.run(run()) if r["outcome"] in ("over_limit", "ok")] == []


def test_the_fake_drops_requests_above_50_a_second_when_asked_to():
    async def run():
        fake = FakeBMS()
        fake.enforce_rate = True
        answered = 0
        for _ in range(20):                                        # 3 registers each: the 17th passes 50
            try:
                await asyncio.wait_for(fake.read_input_registers(1000, count=3), 0.05)
                answered += 1
            except asyncio.TimeoutError:
                pass
        return answered, fake
    answered, fake = asyncio.run(run())
    assert answered == 16 and len(fake.dropped) == 4               # dropped: never answered, never queued
