"""
capstone/bms_probe.py
Read-only check of a real Endurium BMS (SPC100644) over Modbus TCP.

    python -m capstone.bms_probe --host 192.168.1.50

It connects, READS about thirty registers and prints a plain-English PASS / WARN /
FAIL line for each, so you can compare the numbers with the site's own screens
before the gateway ever sends a command. It never writes: only read_input_registers
and read_holding_registers are used (tests/test_bms_probe.py proves it against a
fake BMS that records every write). The report is also saved under probe_reports/.

Exit status: 0 no FAIL, 1 at least one FAIL, 2 could not connect at all.
"""

import argparse
import asyncio
import datetime
import os
import sys

from pymodbus.client import AsyncModbusTcpClient

from capstone import modbus_driver as d

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
PAUSE_SEC = 0.15          # between requests: far below the BMS limit of 50 registers a second (5.2)
HEARTBEAT_WAIT_SEC = 2.2  # 31003 increments once a second

ALERT_NAMES = {6: "No Active Controller"}   # the one 31039 bit the ICD names that the gateway uses


class Check:
    def __init__(self, name, status, value, meaning):
        self.name, self.status, self.value, self.meaning = name, status, value, meaning


async def _read(client, kind, address, count, unit):
    """One read: (registers, None) on success or (None, why) on failure."""
    await asyncio.sleep(PAUSE_SEC)
    method = client.read_input_registers if kind == "input" else client.read_holding_registers
    try:
        result = await asyncio.wait_for(method(address, count=count, slave=unit), timeout=d.PROBE_TIMEOUT_SEC + 1)
    except asyncio.TimeoutError:
        return None, "no answer (timed out)"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    if result is None:
        return None, "no answer"
    if result.isError():
        return None, f"the BMS refused it ({getattr(result, 'exception_code', result)})"
    return list(result.registers), None


async def find_unit(client, preferred, checks):
    """Try the unit ID you gave, then 1 and 0 (as the gateway does at connect)."""
    candidates = list(dict.fromkeys([preferred, *d.PROBE_UNIT_IDS]))
    for unit in candidates:
        regs, _ = await _read(client, "input", d.REG_BMS_TYPE, 1, unit)
        if regs is not None:
            note = "" if unit == preferred else f" (not {preferred}: use MODBUS_UNIT_ID={unit})"
            checks.append(Check("Unit ID", PASS, str(unit), f"The BMS answers on unit ID {unit}{note}."))
            return unit, regs
    checks.append(Check("Unit ID", FAIL, "no answer",
                        f"Nothing answered on unit IDs {', '.join(map(str, candidates))}. "
                        "Check the IP and port, and ask what unit ID the BMS uses."))
    return None, None


async def run_checks(client, unit_id=d.DEFAULT_UNIT_ID, heartbeat_wait=HEARTBEAT_WAIT_SEC):
    """Read the BMS and return a list of Check. `client` is any pymodbus-style client that is
    already connected; only its two read methods are ever called."""
    checks = []
    unit, type_regs = await find_unit(client, unit_id, checks)
    if unit is None:
        return checks

    bms_type = type_regs[0]
    if bms_type in (d.BMS_TYPE_DC, d.BMS_TYPE_AC):
        kind_name = "DC-configured" if bms_type == d.BMS_TYPE_DC else "AC-configured"
        extra = (" The gateway reads it but does not dispatch it (the plant controller drives it)."
                 if bms_type == d.BMS_TYPE_DC else " The gateway writes its active-power setpoint (41009).")
        checks.append(Check("System type (31038)", PASS, kind_name,
                            "It tells the gateway which setpoint register applies." + extra))
    else:
        checks.append(Check("System type (31038)", WARN, str(bms_type),
                            "The spec defines only 0 = DC and 1 = AC. The gateway would refuse to send any setpoint. Ask Invinity."))

    regs, why = await _read(client, "input", d.REG_OPERATING_STATE, 2, unit)
    if regs is None:
        checks.append(Check("Operating state (31000)", FAIL, "unreadable", why))
    else:
        state, status = regs
        sname = d.OPERATING_STATE_NAMES.get(state)
        tname = d.OPERATING_STATUS_NAMES.get(status)
        checks.append(Check("Operating state (31000)", PASS if sname and state != 10 else WARN, sname or f"code {state}",
                            "What the battery is doing now." if sname and state != 10
                            else "The battery is in Fault: do not send commands; ask Invinity first." if state == 10
                            else "A state code the spec table does not list."))
        if status in (3, 4) or state == 10:
            checks.append(Check("Health (31001)", WARN, tname,
                                "The BMS reports a fault. Do not send commands; ask Invinity first."))
        elif status == 2:
            checks.append(Check("Health (31001)", WARN, tname,
                                "A warning is active. Ask Invinity what it is before commanding."))
        else:
            checks.append(Check("Health (31001)", PASS if tname else WARN, tname or f"code {status}",
                                "Overall health as the BMS reports it."))

    regs, why = await _read(client, "input", d.REG_CONTROL_MODE, 1, unit)
    if regs is None:
        checks.append(Check("Control mode (31036)", FAIL, "unreadable", why))
    elif regs[0] == d.CONTROL_USER:
        checks.append(Check("Control mode (31036)", WARN, "User-Controlled",
                            "Someone is controlling it by hand. The BMS would accept our writes but NOT store them (spec 6.1)."))
    else:
        checks.append(Check("Control mode (31036)", PASS if regs[0] == d.CONTROL_NORMAL else WARN,
                            d.CONTROL_MODE_NAMES.get(regs[0], f"code {regs[0]}"),
                            "Normal means it will accept control from the gateway."))

    regs, why = await _read(client, "input", d.REG_STATE_OF_CHARGE, 1, unit)
    if regs is None:
        checks.append(Check("Battery charge (31004)", FAIL, "unreadable", why))
    else:
        pct = regs[0] / 10
        ok = 0 <= pct <= 100
        checks.append(Check("Battery charge (31004)", PASS if ok else FAIL, f"{pct:.1f} %",
                            "Compare this with Invinity's screen: it should match." if ok
                            else "Not a percentage: the scaling (x10) may differ."))

    first, why = await _read(client, "input", d.REG_HEARTBEAT, 1, unit)
    if first is None:
        checks.append(Check("BMS heartbeat (31003)", FAIL, "unreadable", why))
    else:
        await asyncio.sleep(heartbeat_wait)
        second, why = await _read(client, "input", d.REG_HEARTBEAT, 1, unit)
        if second is None:
            checks.append(Check("BMS heartbeat (31003)", FAIL, "unreadable", why))
        elif second[0] != first[0]:
            checks.append(Check("BMS heartbeat (31003)", PASS, f"{first[0]} -> {second[0]}",
                                "It is counting, so the BMS is alive. (The gateway echoes this number back; this check does not.)"))
        else:
            checks.append(Check("BMS heartbeat (31003)", FAIL, f"stuck at {first[0]}",
                                "It should count up about once a second. Stuck means the BMS is not healthy; the gateway would refuse to dispatch."))

    for label, address, signs, unit_name in (
            ("DC power (31007)", d.REG_DC_POWER, "positive = discharging", "kW"),
            ("AC active power (31009)", d.REG_ACTIVE_POWER, "positive = exporting to the grid", "kW"),
            ("Reactive power (31011)", d.REG_REACTIVE_POWER, "sign convention is not in the spec", "kVAR")):
        regs, why = await _read(client, "input", address, 2, unit)
        if regs is None:
            checks.append(Check(label, WARN, "unreadable", why))
        else:
            checks.append(Check(label, INFO, f"{d._decode_int32(regs)} {unit_name}",
                                f"Measured now ({signs}). Compare with their monitor, including the sign."))

    regs, why = await _read(client, "input", d.REG_CHARGE_POWER_LIMIT, 4, unit)
    if regs is None:
        checks.append(Check("Power limits (31019/31021)", FAIL, "unreadable",
                            why + " The gateway cannot send a setpoint without these."))
    else:
        charge, discharge = d._decode_int32(regs[0:2]), d._decode_int32(regs[2:4])
        ok = charge <= 0 <= discharge
        checks.append(Check("Power limits (31019/31021)", PASS if ok else WARN,
                            f"charge up to {-charge} kW, discharge up to {discharge} kW",
                            "The most power allowed right now. Zero means it is not allowed to move (e.g. a fault)." if ok
                            else "The signs are not as the spec says (charge <= 0 <= discharge). Check with Invinity."))

    regs, why = await _read(client, "input", d.REG_BMS_ALERTS, 2, unit)
    if regs is None:
        checks.append(Check("Alerts (31039)", WARN, "unreadable", why))
    else:
        raw = (regs[0] << 16) | regs[1]
        bits = [b for b in range(32) if raw >> b & 1]
        if not bits:
            checks.append(Check("Alerts (31039)", PASS, "none", "No alert bits are set."))
        else:
            names = ", ".join(f"bit {b}" + (f" ({ALERT_NAMES[b]})" if b in ALERT_NAMES else "") for b in bits)
            checks.append(Check("Alerts (31039)", WARN, names,
                                "Alert bits are set. Ask Invinity what they mean before commanding. "
                                "'No Active Controller' is normal while only watching."))

    # Holding registers: what is currently commanded. Read only.
    regs, why = await _read(client, "holding", d.REG_OPERATING_COMMAND, 1, unit)
    if regs is None:
        checks.append(Check("Operating command (41000)", INFO, "unreadable", why))
    else:
        checks.append(Check("Operating command (41000)", INFO,
                            {0: "Off", 1: "Standby", 2: "On"}.get(regs[0], f"code {regs[0]}"),
                            "What a controller last asked for. This check does not change it."))
    for label, address in (("Operator charge cap (41014)", d.REG_OPERATING_CHARGE_CAP),
                           ("Operator discharge cap (41016)", d.REG_OPERATING_DISCHARGE_CAP)):
        regs, why = await _read(client, "holding", address, 2, unit)
        if regs is None:
            checks.append(Check(label, INFO, "unreadable", why))
        else:
            kw = d._decode_int32(regs)
            checks.append(Check(label, INFO, "no cap set" if kw == 0 else f"{kw} kW",
                                "A limit set by the operator (0 = none). The gateway stays within it."))
    if bms_type == d.BMS_TYPE_AC:
        regs, why = await _read(client, "holding", d.REG_GRID_CONTROL_SCOPE, 4, unit)
        if regs is None:
            checks.append(Check("Dispatch modes (41018-41021)", INFO, "unreadable", why))
        else:
            scope, grid, active, reactive = regs
            text = (f"scope {d.SCOPE_NAMES.get(scope, scope)}, grid {d.GRID_MODE_NAMES.get(grid, grid)}, "
                    f"active {d.ACTIVE_MODE_NAMES.get(active, active)}, "
                    f"reactive {d.REACTIVE_MODE_NAMES.get(reactive, reactive)}")
            invalid = 0 in (grid, active, reactive)      # 0 is a real scope (System-Level) but Invalid for the others
            checks.append(Check("Dispatch modes (41018-41021)", INFO, text,
                                "Zero (Invalid) means the gateway would set these when it takes over."
                                if invalid else "Already set by someone; the gateway leaves valid settings alone."))
    return checks


def format_report(checks, host, port):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"BMS read-only check   {host}:{port}   {stamp}", "Nothing was written to the BMS.", ""]
    for c in checks:
        lines.append(f"[{c.status}] {c.name}: {c.value}")
        lines.append(f"       {c.meaning}")
    counts = {s: sum(1 for c in checks if c.status == s) for s in (PASS, WARN, FAIL)}
    lines += ["", f"Result: {counts[PASS]} pass, {counts[WARN]} warning, {counts[FAIL]} fail"]
    if counts[FAIL]:
        lines.append("Do not send any command yet. Fix or ask about the FAIL lines first.")
    elif counts[WARN]:
        lines.append("Readings work. Ask Invinity about the warnings before any command.")
    else:
        lines.append("All readings work. Now compare each number with Invinity's screen.")
    return "\n".join(lines)


async def probe(host, port, unit, timeout):
    """Connect and run the checks; None when the connection could not be made."""
    client = AsyncModbusTcpClient(host=host, port=port, timeout=timeout)
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout + 1)
    except Exception:
        pass
    if not client.connected:
        client.close()
        return None
    try:
        return await run_checks(client, unit)
    finally:
        client.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only check of an Endurium BMS (nothing is ever written).")
    ap.add_argument("--host", required=True, help="the BMS IP address Invinity gave you")
    ap.add_argument("--port", type=int, default=502, help="Modbus TCP port (default 502)")
    ap.add_argument("--unit", type=int, default=d.DEFAULT_UNIT_ID,
                    help="Modbus unit ID (default 255; 1 and 0 are tried too)")
    ap.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for a connection (default 5)")
    ap.add_argument("--no-save", action="store_true", help="do not save the report under probe_reports/")
    args = ap.parse_args(argv)

    print(f"Connecting to {args.host}:{args.port} (read-only)...")
    checks = asyncio.run(probe(args.host, args.port, args.unit, args.timeout))
    if checks is None:
        print(f"\n[FAIL] Could not connect to {args.host}:{args.port}.\n"
              f"  1. Are you on Invinity's network? Try:  ping {args.host}\n"
              "  2. Is the port right? Ask Invinity (usually 502).\n"
              "  3. Is a firewall blocking it? Ask Invinity. Do not try other addresses or ports.")
        return 2
    report = format_report(checks, args.host, args.port)
    print("\n" + report)
    if not args.no_save:
        os.makedirs("probe_reports", exist_ok=True)
        name = f"bms_probe_{args.host.replace(':', '_')}_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt"
        path = os.path.join("probe_reports", name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(report + "\n")
        print(f"\nSaved: {path}")
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
