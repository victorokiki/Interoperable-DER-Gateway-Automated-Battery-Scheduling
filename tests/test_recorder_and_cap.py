"""
tests/test_recorder_and_cap.py
Two aids for the first tests on real hardware.

BMS_MAX_POWER_KW: a ceiling of our own on every setpoint, whatever the BMS allows and whoever asks
(the dashboard's Run now defaults to 500 kW). Applied where the limits are produced, so the written
value, the published limits and the audit rows all agree.

The recorder: a CSV of what the BMS itself measured (not only what the gateway sent), read-only.
"""

import asyncio
import csv

import pytest

from capstone import __main__ as gateway_main
from capstone.recorder import COLUMNS, Recorder
from fakes import FakeBMS, make_driver


def run(coro):
    return asyncio.run(coro)


# --- the power cap -----------------------------------------------------------------------------

def written_after(power_kw, **settings):
    async def go():
        fake = FakeBMS(bms_type=1)
        driver, _ = await make_driver(fake, **settings)
        ok = await driver.set_power(power_kw, source="test")
        return ok, fake.setpoint_kw(), driver.limits_kw
    return run(go())


def test_the_cap_holds_a_big_charge_and_a_big_discharge_to_the_cap():
    ok, kw, limits = written_after(500.0, bms_max_power_kw=5)
    assert ok and kw == 5 and limits == (5, 5)
    ok, kw, _ = written_after(-500.0, bms_max_power_kw=5)
    assert ok and kw == -5


def test_a_request_under_the_cap_is_written_as_asked():
    assert written_after(3.0, bms_max_power_kw=5)[1] == 3


def test_zero_means_no_cap_of_our_own():
    ok, kw, limits = written_after(500.0, bms_max_power_kw=0)
    assert ok and kw == 500 and limits == (10_000, 10_000)         # FakeBMS publishes 10 000 kW


def test_the_cap_never_raises_a_limit_the_bms_set_lower():
    async def go():
        fake = FakeBMS(bms_type=1)
        fake.set_limits(charge_kw=2, discharge_kw=2)
        driver, _ = await make_driver(fake, bms_max_power_kw=5)
        await driver.set_power(500.0, source="test")
        return fake.setpoint_kw()
    assert run(go()) == 2


def test_stopping_is_not_affected_by_the_cap():
    async def go():
        driver, _ = await make_driver(FakeBMS(bms_type=1), bms_max_power_kw=1)
        return await driver.set_power(0, source="test")
    assert run(go()) is True


def test_the_cap_is_audited_in_the_limits_of_each_write(tmp_path):
    from capstone.audit_log import AuditLog
    from fakes import audit_rows

    async def go():
        audit = AuditLog(db_path=tmp_path / "audit.db")
        driver, _ = await make_driver(FakeBMS(bms_type=1), audit_log=audit, bms_max_power_kw=5)
        await driver.set_power(500.0, source="test")
        audit.flush()
        return audit_rows(audit)
    rows = run(go())
    assert any("requested=+500.0 kW written=+5.0 kW" in r["detail"] and "charge<=5 discharge<=5" in r["detail"]
               for r in rows if r["event_type"] == "modbus_write"), [r["detail"] for r in rows]


def test_the_config_reads_and_checks_the_new_settings(monkeypatch):
    for k in ("BMS_MAX_POWER_KW", "RECORD_BMS", "RECORD_INTERVAL_SEC", "RECORD_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DER_SET_MAX_W", "50000")
    cfg = gateway_main.load_config()
    assert cfg["bms_max_power_kw"] == 0 and cfg["record_bms"] is False and cfg["record_interval_sec"] == 2
    monkeypatch.setenv("BMS_MAX_POWER_KW", "5")
    monkeypatch.setenv("RECORD_BMS", "1")
    cfg = gateway_main.load_config()
    assert cfg["bms_max_power_kw"] == 5 and cfg["record_bms"] is True
    monkeypatch.setenv("BMS_MAX_POWER_KW", "-1")
    with pytest.raises(gateway_main.ConfigError):
        gateway_main.load_config()
    monkeypatch.setenv("BMS_MAX_POWER_KW", "5")
    monkeypatch.setenv("RECORD_INTERVAL_SEC", "0")
    with pytest.raises(gateway_main.ConfigError):
        gateway_main.load_config()


# --- the recorder ------------------------------------------------------------------------------

async def record_for(driver, state, directory, seconds, interval=0.1):
    rec = Recorder(driver, state, directory, interval)
    task = asyncio.create_task(rec.run())
    await asyncio.sleep(seconds)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return rec


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_the_recorder_writes_the_measured_power_next_to_the_commanded_setpoint(tmp_path):
    async def go():
        fake = FakeBMS(bms_type=1)
        driver, state = await make_driver(fake)
        await driver.set_power(3.0, source="test")
        rec = await record_for(driver, state, tmp_path, 0.6)
        return rec, fake
    rec, fake = run(go())
    rows = read_rows(rec.path)
    assert list(rows[0].keys()) == COLUMNS and len(rows) >= 3
    last = rows[-1]
    assert last["bms_state"] == "On" and last["control_mode"] == "Normal"
    assert float(last["measured_kw"]) == 3.0 and float(last["confirmed_setpoint_kw"]) == 3.0
    assert last["setpoint_source"] == "test" and last["utc"].endswith("Z") and last["alerts_hex"] == "0x00000000"


def test_the_recorder_only_reads(tmp_path):
    async def go():
        fake = FakeBMS(bms_type=1)
        driver, state = await make_driver(fake)
        before = len(fake.writes)
        await record_for(driver, state, tmp_path, 0.5)
        return len(fake.writes) - before
    assert run(go()) == 0


def test_a_failed_read_is_a_row_with_a_note_not_a_crash(tmp_path):
    async def go():
        fake = FakeBMS(bms_type=1)
        driver, state = await make_driver(fake)
        fake.link_down = True
        return await record_for(driver, state, tmp_path, 0.4)
    rows = read_rows(run(go()).path)
    assert rows and all(r["note"] and r["measured_kw"] == "" for r in rows)


def test_the_recorder_keeps_every_row_in_the_file_as_it_goes(tmp_path):
    """Flushed row by row, so a crash or a pulled plug still leaves everything recorded so far."""
    async def go():
        driver, state = await make_driver(FakeBMS(bms_type=1))
        rec = Recorder(driver, state, tmp_path, 0.1)
        task = asyncio.create_task(rec.run())
        await asyncio.sleep(0.45)
        rows_while_running = read_rows(rec.path)          # read before it is stopped
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return rows_while_running
    assert len(run(go())) >= 3
