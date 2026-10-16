# Frankensystem results log (16 Oct 2026, Vancouver time)

Running log of the merged test plan (`FRANKENSYSTEM_TEST_PLAN.md`). One entry per test: result, evidence file, anything odd.
BMS 10.4.56.163, control port 5020, read-only monitoring port 5030, unit ID 255, AC-configured, SPC100644 Rev 05 (draft).
Our caps: BMS_MAX_POWER_KW=5. Battery at the time: On, health Warning (accepted by Invinity), control mode Normal, charge about 57 %.

## Test 1: Connect and identify: PASS (read-only gateway, 11:34)
Unit ID 255 resolved; AC-configured detected (31038 = 1); state On, control Normal, alerts 0x00000000; dispatch modes already in place;
operator caps 50 kW each way read on the control port; heartbeat counting. 0 link drops, 0 writes confirmed, 1 write refused by read-only.
Evidence: audit log (gateway start 18:34:09 UTC), recordings\bms_20261016_183409.csv.

## Test 2: Register survey: PASS on values, two items open (11:30, 11:34)
Engineer confirmed every value matched their display (state, health, charge, power, limits 84 kW charge / 189 kW discharge, control mode, alerts, type).
41009 sign confirmed by the engineer: negative = charge, positive = discharge.
Open: which alert bit 5 meant (Array Comms Lost or No Active Controller, numbering ambiguous in Rev 05); whether an operator cap of 0 means no cap or zero.
Finding: the monitoring port (5030) shows zeros for the 41xxx control registers; read them on the control port.
Evidence: probe_reports\bms_probe_10.4.56.163_20261016_113051.txt and the audit rows above.

## Test 3: Heartbeat, 10 minutes idle: PASS (writes enabled, 11:47:35 to 11:57:57)
622 s, 307 recorded samples, 0 read problems. The BMS counter advanced 622 ticks in 622 s. Our echo (41002) followed it with a lag of 0 or 1 s in every sample.
Alerts 0x00000000 in all 307 samples; state On / Warning / Normal throughout; heartbeat-stale flag never set; 0 link drops, 0 alarms.
Only one write confirmed (the 0 kW setpoint at start-up); no non-zero setpoint. Register load: average 10.3 a second, peak 48 (see below).
Evidence: recordings\bms_20261016_184735.csv (columns bms_heartbeat_31003 / operator_heartbeat_41002 / alerts_hex), audit log.

Oddities, recorded honestly:
- **A 10 s stall at 11:49:21 to 11:49:31.** The recorded counter and our echo both stood still for about 10 s, then caught up (+12 ticks). Nothing was logged: no
  "connection lost", no error, no audit row. All three loops (recorder, heartbeat, status) stalled together, which points at the host or the network, not at the BMS.
  The BMS raised **no alert** during the gap, so its comms-lost timeout is **longer than about 10 s** (useful for test 9).
- **Register peak 48 a second at 11:49:33**: the catch-up burst right after that stall (steady state is about 10 a second; the earlier peak was 39). Under 50, but closer than we want. Test 10 should note it.
- **11:53:04 WARNING** `Could not publish status snapshot ... live_status.tmp Access is denied` (the known Windows file-replace race with the dashboard reading the file; one occurrence, retried on the next cycle, no effect on the battery).

## Test 5: Low-power charge: PASS (12:02:43 to 12:03:38)
Event F0FF2C67, charge 3 kW, 60 s window. Written -3 kW to 41009 (hardware negative = charge, confirmed by the engineer), read back exact, BMS On / Normal.
BMS-measured power 3 kW from the first sample after the write, steady to the end; 0 kW written and measured at 12:03:38, the instant the window ended. No alerts.
Power began 5 s after the window start (the gateway polls the utility every 5 s); charge level unchanged at 56.9 % (about 0.05 kWh is below its resolution).
Evidence: recordings\bms_20261016_184735.csv (measured_kw), audit log (modbus_write rows). Engineer confirmation of 3 kW into the battery: to be noted.

## Test 6: Low-power discharge: PASS (12:05:18 to 12:06:14), one observation
Event 64A3D119, discharge 3 kW. Written +3 kW to 41009, read back exact, BMS On / Normal; BMS-measured power negative (discharging), as expected for the sign.
**Observation: the BMS measured -2 kW for the whole discharge against a +3 kW setpoint** (the charge measured exactly 3). Cause unknown from our side (losses / auxiliary load / rounding / delivered less than asked); engineer to say what their screen showed.
Return to 0: 0 kW written at 12:06:14; measured 0 by 12:06:17. Charge level 56.9 to 56.6 % during the minute (the BMS's estimate looks coarse; not interpreted).
Evidence: the same recording and audit log.
