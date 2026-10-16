"""
tests/test_bms_probe.py
The read-only check (capstone/bms_probe.py) and BMS_READ_ONLY.

Both exist for the first connection to real hardware, so what they must never do is
tested hardest: no register of the BMS is ever written. FakeBMS records every write it
stores; the tests assert that list stays empty.
"""

import asyncio

import pytest

from capstone import bms_probe
from capstone.bms_probe import FAIL, PASS, WARN, run_checks
from capstone.modbus_driver import OPERATING_COMMAND_ON, REG_OPERATING_COMMAND, ReadOnlyError
from fakes import FakeBMS, make_driver


def probe(fake, **kw):
    return asyncio.run(run_checks(fake, heartbeat_wait=1.1, **kw))


def by_name(checks, start):
    return next(c for c in checks if c.name.startswith(start))


def test_a_healthy_ac_bms_passes_and_nothing_is_written():
    fake = FakeBMS(bms_type=1)
    checks = probe(fake)
    assert not [c for c in checks if c.status == FAIL]
    assert by_name(checks, "System type").value == "AC-configured"
    assert by_name(checks, "Battery charge").value == "50.0 %"
    assert by_name(checks, "BMS heartbeat").status == PASS
    assert fake.writes == [] and fake.ignored == []


def test_a_dc_bms_is_reported_as_read_only_for_dispatch():
    checks = probe(FakeBMS(bms_type=0))
    assert by_name(checks, "System type").value == "DC-configured"
    assert not any(c.name.startswith("Dispatch modes") for c in checks)


def test_a_wrong_unit_id_is_found_and_named():
    fake = FakeBMS()
    fake.answers_units = {1}
    checks = probe(fake, unit_id=255)
    unit = by_name(checks, "Unit ID")
    assert unit.status == PASS and unit.value == "1" and "MODBUS_UNIT_ID=1" in unit.meaning


def test_no_answer_on_any_unit_id_is_a_fail_and_stops_early():
    fake = FakeBMS()
    fake.answers_units = set()
    fake.other_units = "silent"
    checks = probe(fake)
    assert [c.status for c in checks] == [FAIL]


def test_a_frozen_heartbeat_fails():
    fake = FakeBMS()
    fake.heartbeat_live = False
    assert by_name(probe(fake), "BMS heartbeat").status == FAIL


def test_a_fault_and_user_controlled_mode_are_warnings_not_passes():
    fake = FakeBMS()
    fake.fault()
    fake.user_controlled(True)
    checks = probe(fake)
    assert by_name(checks, "Operating state").status == WARN and by_name(checks, "Health").status == WARN
    assert by_name(checks, "Control mode").status == WARN


def test_a_charge_percentage_outside_0_to_100_fails():
    fake = FakeBMS()
    fake.ir[1004] = 5000
    assert by_name(probe(fake), "Battery charge").status == FAIL


def test_the_probe_stays_under_the_bms_register_rate_limit():
    fake = FakeBMS()
    fake.enforce_rate = True
    probe(fake)
    assert fake.dropped == []


def test_the_probe_never_calls_a_write_method():
    class ReadsOnly(FakeBMS):
        async def write_registers(self, *a, **k):
            raise AssertionError("the probe tried to write")

        async def write_register(self, *a, **k):
            raise AssertionError("the probe tried to write")

    assert probe(ReadsOnly())


def test_the_report_says_nothing_was_written_and_gives_a_verdict():
    text = bms_probe.format_report(probe(FakeBMS()), "10.0.0.5", 502)
    assert "Nothing was written to the BMS." in text and "10.0.0.5:502" in text and "Result:" in text


# --- BMS_READ_ONLY in the gateway driver --------------------------------------

def test_read_only_driver_connects_and_reads_but_writes_nothing():
    async def run():
        fake = FakeBMS(operating_state=2)          # Standby: a normal driver would now send On
        driver, state = await make_driver(fake, bms_read_only=True)
        status = await driver.read_status()
        ok = await driver.set_power(5.0, source="test") if hasattr(driver, "set_power") else None
        await driver.heartbeat_once()
        return fake, status, ok
    fake, status, ok = asyncio.run(run())
    assert fake.writes == [] and fake.ignored == []
    assert status                                  # reads still work
    assert not ok                                  # a dispatch is refused, not confirmed


def test_read_only_refuses_a_direct_write_with_a_clear_error():
    async def run():
        driver, _ = await make_driver(FakeBMS(), bms_read_only=True)
        with pytest.raises(ReadOnlyError):
            await driver._client.write_registers(REG_OPERATING_COMMAND, [OPERATING_COMMAND_ON], slave=255)
    asyncio.run(run())


def test_without_read_only_the_driver_still_writes():
    async def run():
        fake = FakeBMS(operating_state=2)
        await make_driver(fake)
        return fake
    assert asyncio.run(run()).commands()          # sent On to a Standby BMS


def test_system_level_scope_zero_is_not_reported_as_invalid_dispatch_modes():
    """Found on real hardware: scope 0 is System-Level, a valid value, not Invalid."""
    fake = FakeBMS(bms_type=1)
    fake.hr.update({1018: 0, 1019: 1, 1020: 1, 1021: 1})
    modes = by_name(probe(fake), "Dispatch modes")
    assert "System-Level" in modes.value and "leaves valid settings alone" in modes.meaning

    fake.hr[1020] = 0                                    # Fixed Active Power not set: that one is Invalid
    assert "would set these" in by_name(probe(fake), "Dispatch modes").meaning


def test_a_set_alert_bit_names_both_readings_of_the_specs_row_numbers():
    """Rev 05 numbers the 31039 rows 1 to 32 but counts other bitfields from 0, so say both."""
    fake = FakeBMS()
    fake.ir[1040] = 1 << 5                                   # 31039-31040 as one uint32: bit 5 set
    alerts = by_name(probe(fake), "Alerts")
    assert alerts.status == WARN
    assert "Array Comms Lost" in alerts.value and "No Active Controller" in alerts.value


def test_no_alert_bits_is_a_pass():
    assert by_name(probe(FakeBMS()), "Alerts").status == PASS


def test_on_the_monitoring_port_the_control_registers_are_not_reported():
    """Found on the real BMS: port 5030 shows zeros for 41xxx, so the cap and dispatch-mode lines misled."""
    checks = probe(FakeBMS(bms_type=1), skip_holding=True)
    assert by_name(checks, "Control registers").value == "not read"
    assert not any(c.name.startswith(("Operator", "Dispatch modes", "Operating command")) for c in checks)
