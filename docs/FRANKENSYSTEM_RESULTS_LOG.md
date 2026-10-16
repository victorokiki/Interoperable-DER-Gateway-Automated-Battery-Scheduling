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

## Test 7 (and 7b): Supersede, Force Idle, Release: PASS (12:08:08 to 12:11:02)
Sequence from the dashboard (cap 5 kW, all 3 kW): charge 300 s (CDAAEA9C, 12:08:08) -> discharge 60 s with "replace overlaps" (59C342C9, 12:08:48) -> CLEAR QUEUE AND FORCE IDLE (12:09:04) -> RELEASE (12:09:24) -> charge 60 s (AE95A219, 12:10:00, ended 12:11:00).
Supersede: the charge was cancelled by the utility side (EventStatus 2) and the discharge took authority; BMS-measured power flipped from +2 to -2 kW within about 3 s.
Force Idle: 0 kW written and confirmed within 1 s of the click; measured 0 kW; the discharge event was cancelled mid-event (7b: power really stopped). Held for **about 19 s** (planned longer; a wait command was blocked).
Release: control resumed (authority back to the schedule; the next event ran 3 kW and ended on time with 0 kW written and measured).
5 writes (3, -3, 0, 3, 0 kW): all read back and confirmed; no alerts; state On / Normal throughout.
Observation: measured power read 2 or 3 kW for a 3 kW setpoint (charge 3, then 2, then 3; discharge 2 both times): consistent with a real value near 2.5 kW in a whole-kW register (+/- 1 kW); engineer's screen may show decimals.
Dashboard note: the page's layout shifts while its status tiles load, so a click or a typed value placed by screen position could land on the wrong field; the test steps were therefore sent with the values set and the button pressed in one step. (An earlier version of this note said the form resets itself when the page refreshes; that was wrong: the form is static and the refresh code never touches it.) The Run now form defaults to 500 kW and 3600 s and enforces a 60 s minimum.
Evidence: recordings\bms_20261016_184735.csv, audit log rows 19:08 to 19:11 UTC.

## Test 8: Utility link loss: PASS (12:15:44 to 12:22:20)
Event 5EE28EE3, charge 3 kW, 12:15:40 to 12:19:40. Server (utility link) stopped at about 12:16:30 and restarted at about 12:21:25.
12:16:35 CONNECTED -> DERATED (first failed poll); 12:16:45 DERATED -> AUTONOMOUS (running the approved event unaided). 59 failed polls while down.
BMS-measured power continuous (2 kW for a 3 kW setpoint, as before) through the whole outage; event ended 12:19:40: 0 kW written and confirmed, AUTONOMOUS -> SAFE_IDLE the same second, measured 0 by 12:19:43.
Restore: SAFE_IDLE -> CONNECTED at 12:21:29. The EventCompleted reply that failed (12:19:43) was retried with back-off (5 failed attempts) and delivered at 12:22:18 (HTTP 201); the failed DERStatus was resent at 12:22:20. Nothing lost.
No BMS alerts, heartbeat never stale, no BMS link problems during the outage. Evidence: audit rows 19:15 to 19:22 UTC, recordings\bms_20261016_184735.csv.

Engineer's observation on test 8 (utility link cut): the battery kept its 3 kW charge and went to 0 kW when the event ended. Their screen showed 3 kW where our register read 2 (whole-kW register).

## Test 9: BMS link loss: loss detection and recovery PASS; significant finding on BMS behaviour (12:25:44 to 12:29:57)
Event F03FF595, charge 3 kW, 12:25:44 to 12:29:44. The laptop's Ethernet cable was unplugged at about 12:26:45 (first read the BMS did not answer) and re-plugged at about 12:28:15: **about 92 s out, not the 60 s planned**.
Detected and logged: first failed dispatch 12:27:04 (limits unreadable), "connection lost, reconnecting" 12:27:07 (22 s after the first failed read).
After the cable was back, "reconnected after 8 attempts" at 12:28:17, but the connection then **dropped and re-formed 8 times, about every 12 s (12:28:18 to 12:29:42)**: TCP connected but the BMS did not answer (unit 255 no answer in 2 s), so the gateway could not read the BMS type and **could not write** (154 failed dispatch attempts).
Stable at **12:29:49 (about 184 s after the cut)**: BMS type detected, **0 kW written and confirmed**, 5 s after the event ended (12:29:44). BMS-measured power 0 by 12:29:51.
**BMS behaviour observed:** it kept charging (measured 2 kW at the first sample after reconnect, 12:29:49), i.e. **it held the last setpoint for at least about 184 s with no controller**, and showed **no alert** (alerts 0x00000000 straight after reconnect). Its single-connection control port appears to have kept the dead session for about 3 minutes, accepting but not serving new connections until that expired.
Consequence: if the stale session outlasts an event, the gateway cannot stop the battery and it keeps its last setpoint. Recovery here was luck of timing (the session cleared just after the event end).
Limits of the evidence: our recording is blank 12:26:45 to 12:29:47 (no link). Open, for the engineer: when their screen showed 0 kW (before or after 12:29:49); any alarm and when; the control-connection inactivity timeout; whether holding the last setpoint is intended.
Evidence: audit rows 19:26 to 19:30 UTC; recordings\bms_20261016_184735.csv (82 rows with a note, 12:26:45 to 12:29:47).

## Test 4: Start-up sequence (Standby to On): PASS (12:33 to 12:39), with test 4b
The engineer put the battery in Standby with their service tool, which switched the BMS to User-Controlled (12:33:26), then Shutting Down (12:33:30), Standby (12:33:50). The charge and discharge limits (31019/31021) went to 0/0 from 12:33:40 (spec 6.2.4: limits set to 0 before disconnecting).
While User-Controlled the gateway logged the mode once, made one write attempt (refused by the BMS with Modbus exception 6, device busy, while shutting down) and then stopped writing; it never claimed a write was confirmed (this is also the evidence for optional test 11 with no event running).
12:37:20 the engineer released control to Normal; **12:37:21 the gateway commanded On (41000 := 2) within the same second** ("BMS is in Standby: On", a take-over). BMS: Starting up 12:37:22, Waiting for Connection 12:38:33, **On 12:38:37 (76 s after the command)**; limits back to 5/5 (our cap) at 12:38:43. No fault; alerts 0x00000000 in all 206 samples of the window; health Warning throughout (as before).
**4b, the 5 s wait (Rev 05 6.2.4), on the real battery:** a 3 kW charge was sent at 12:38:56. The gateway refused to write while it had seen the BMS On for 0.0, 1.0, 2.0, 3.1 and 4.2 s ("settling", retried each second), then **wrote +3 kW at 12:39:06 (5.2 s) and the BMS confirmed it**; measured 2 kW at 12:39:07; event ended 12:39:56 with 0 kW written and measured 0 at 12:39:57. The clock starts when the gateway first reads On (here 12:39:01, after the BMS had been On since 12:38:37), so the wait is never shorter than 5 s and can be longer.
Evidence: audit rows 19:33 to 19:40 UTC; recordings\bms_20261016_184735.csv.

## Test 10: Load and audit check: load PASS; audit gap found and fixed
Load: 32 418 registers requested in 3 193 s (53 min) = **10.2 a second on average; peak 48 in any 1 s window (limit 50)**. The peak was the catch-up burst after the 10 s stall at 11:49:33; otherwise at most 39.
Audit: 271 rows; all 8 events have a received / active / completed-or-cancelled trail; all 8 non-zero setpoints are audited and confirmed.
**Gap found:** 3 of the 5 BMS state changes in the recording (Shutting Down 12:33:30, Standby 12:33:50, On 12:38:37) had **no audit row**, because only code paths that happened to read 31000 (a command, a write's verification) reported a state change; the status poll did not. Fixed in the gateway (the poll now audits every state and health change); 3 new tests (they fail without the fix). The gateway under test ran the old code, so this gap is real in the test evidence: the recording has the exact times.

## Test 12: Scheduled start at 12:55: PASS (12:55:00 to 12:56:00)
Event 2779007F added at 12:50:36 on the Schedule page ("at a set time", Pacific): charge 3 kW, 12:55:00 to 12:56:00. The gateway held it as scheduled for 4 min 24 s.
**Start: 0 s after the scheduled time** (audit second resolution): active, +3 kW written and confirmed at 19:55:00 UTC; EventStarted reply 19:55:01. BMS-measured power 0 at 12:55:01 and 2 kW at 12:55:03 (the BMS took 1 to 3 s to follow). **End: 0 s after the scheduled end**: window ended, 0 kW written and confirmed at 19:56:00, measured 0 at 12:56:01; EventCompleted reply 19:56:01. No alerts; On / Normal throughout.
Evidence: audit rows 19:50 to 19:56 UTC; recordings\bms_20261016_184735.csv.

## Test 12 repeat (13:00): PASS
Event 0045094D (13:00:00 to 13:01:00, 3 kW charge, server up): started 0 s after the scheduled time, ended 0 s after the scheduled end; EventStarted and EventCompleted delivered. (Meant for test 13A, but the server was not stopped in time, so it became a second clean scheduled start.)

## Test 13A: Utility server down when the scheduled start passes: PASS (event 25F6103A, 13:18:00 to 13:19:00)
Event received at 13:11:18, then the server was stopped (first failed poll 13:11:54, 91 failed polls, restored about 13:19:26). While down and before the start: CONNECTED -> DERATED 13:11:54, -> DISCONNECTED 13:12:05, -> SAFE_IDLE 13:12:19 (nothing active, 0 kW confirmed).
**13:18:00 the event started on its own, 0 s after the scheduled time** (+3 kW written and confirmed; SAFE_IDLE -> AUTONOMOUS), BMS-measured 2 kW from 13:18:00; **13:19:00 ended on time** (0 kW written and confirmed, AUTONOMOUS -> SAFE_IDLE); measured 0 by 13:19:03. Server back: SAFE_IDLE -> CONNECTED at 13:19:30.
Replies: EventStarted failed at 13:18:02 (no server) and was retried (5 failed attempts) and delivered at 13:20:38 together with EventCompleted; nothing lost.
**Finding (register rate): at 13:18:01 the gateway's meter recorded 51 registers requested in 1 s, one over the BMS limit of 50** (SPC100644 5.2: the BMS silently drops the excess). The start of the scheduled event landed on a heartbeat, a recorder sample and the dispatch's own reads in the same second. Only occurrence in the session (the earlier peak was 48). **This revises the test 10 load result: one 1-second window exceeded 50.** The meter only detected it. Fixed: the driver now paces its requests so no 1 s window holds more than 45 registers (it waits rather than asking), with tests; the audit rows for it remain as a detector (pacing can be switched off for tests).
Evidence: audit rows 20:11 to 20:21 UTC; recordings\bms_20261016_184735.csv.

## Setup before 13B (14:37 to 15:03)
The old gateway was stopped and the stack restarted (new code: register pacing, state-change audit) in visible terminal tabs. The battery had been switched Off by the engineer. They put it in Standby (the BMS then reports control mode User-Controlled, which only their service interface can change: 31036 is read-only, spec 6.1; confirmed by two independent reads, gateway and monitoring port 5030, while the release had not yet taken effect). After the release at 15:00:14 the gateway wrote Standby first (41000 already held On: the E2 safeguard, seen on hardware for the first time) then On at 15:00:21; BMS Starting up 15:00:31, Waiting for Connection 15:01:33, On 15:01:42; limits 86 / 188 kW.

## Test 13B: Battery link down through the whole event window: PASS (event 54318658, 15:12:00 to 15:13:00)
Event received 15:03:09. Cable out about 15:09:06 (first read unanswered), "connection lost, reconnecting" logged 15:09:27; battery idle at 0 kW throughout.
15:12:00 the event became active but its setpoint could not be written (dispatch modes unreadable, ConnectionException). **15:13:00 status EXPIRED ("window ended without its setpoint ever being applied")**. The utility saw only EventReceived (no Started, no Completed).
Cable back about 15:15:00; **15:15:19 reconnected after 35 attempts and stayed connected (no flapping, unlike test 9)**, 0 kW written and confirmed after 350 failed attempts; **no late start, no non-zero setpoint ever written**. The recording has a gap 15:09:06 to 15:15:17 (177 rows with a note); afterwards On / Normal / 0 kW, alerts 0x00000000: **no alert after 6 minutes without a controller**. The echo of the BMS heartbeat (41002) was stale by about 375 ticks at the first sample after reconnect and caught up within 2 s.
Supports the test 9 reading that the BMS holds a dead control connection for about 3 minutes (a 6-minute cut recovered cleanly; the 92 s cut in test 9 flapped).
Engineer's observation of the BMS during the cut: to be noted.
Evidence: audit rows 22:03 to 22:15 UTC; recordings\bms_20261016_214541.csv (the recording of the gateway started 14:45; the earlier ones are bms_20261016_184735.csv for 11:47 to 14:37 and bms_20261016_213816.csv for the read-only start at 14:38).

## Test 13D: Battery link returns inside the event window: PASS (event A6B001B1, 15:30:00 to 15:33:00)
Event received 15:16:45. Cable out 15:23:11 (first read unanswered; "connection lost, reconnecting" 15:23:32), battery idle at 0 kW.
15:30:00 the event became active but could not be written (dispatch modes unreadable). Cable back about 15:31:00. **15:31:13 the gateway reconnected and wrote +3 kW, confirmed by the BMS: 73 s late, 107 s of the window left** (after 457 failed attempts, reconnected after 46 attempts); BMS-measured 2 kW at 15:31:15. **15:33:00 ended exactly on time** (0 kW written and confirmed); the event status is **completed**. Replies: EventStarted sent at the actual start (15:31:14), EventCompleted at 15:33:01. The late start ran only the remaining window; no time was added.
Recording gap 15:23:11 to 15:31:11 (231 rows with a note); no alert after an 8-minute absence of a controller (alerts 0x00000000 at the first sample after reconnect); our heartbeat echo was stale (38061 against 38545) at that sample and caught up within 2 s. Peak register rate 44 per second over the recording (pacing active).
Observation: at 15:31:12 the reliability state went CONNECTED -> DERATED and back to CONNECTED at 15:31:21 (9 s) after two failed utility polls (ClientConnectorError to localhost:5001 at 15:31:12 and 15:31:17) although the server was up; the timing matches Windows bringing the network adapter back after the cable was re-plugged (a network change can briefly disturb even localhost connections). Likely the laptop's adapter, not the utility server; not proven; no effect on the battery.
Evidence: audit rows 22:16 to 22:33 UTC; recordings\bms_20261016_214541.csv.

## Test 13C, first attempt (15:38): not valid; became test 13F
Event 2793F99A (3 kW, 15:41 to 15:42). The Ctrl+C meant for the utility server went to the Gateway tab instead (two tabs, easy to mix up): the **gateway stopped at 15:38:49** (cleanly, final 0 kW confirmed) and the server kept running (0 failed polls). A second server was also started at 15:44 (a duplicate, stopped). The cable was cut and restored, but with the gateway not running nothing could be tested about it.

## Test 13F: Gateway not running when an event's window passes: PASS (event 2793F99A)
The gateway was down from 15:38:49 to 15:49:43 (about 11 min), which included the event window (15:41 to 15:42). The event stayed "scheduled" in the schedule database (nothing was processing it). **At the restart (15:49:43) the gateway ended it immediately as EXPIRED ("window ended without its setpoint ever being applied")**, wrote 0 kW (confirmed) and did not start anything late. The BMS was On / Normal with alerts 0x00000000 at connect: **no alert after about 11 minutes without a controller** (the last setpoint before the stop had been 0 kW).
Evidence: audit rows 22:49:43 UTC.

## Test 13C: Utility server and battery link both down through the event window: PASS (event 7D75F667, 15:56:00 to 15:57:00)
Event received 15:50:24. Server stopped 15:52:12 (first failed poll 15:52:16: CONNECTED -> DERATED -> DISCONNECTED 15:52:26; 87 failed polls until 15:59:26). Cable out about 15:52:06 (first read unanswered), "connection lost, reconnecting" 15:52:28. Battery idle at 0 kW throughout.
15:56:00 the event became active but its setpoint could not be written (dispatch modes unreadable). **15:57:00 status EXPIRED** ("window ended without its setpoint ever being applied"); the utility saw only EventReceived (no Started, no Completed). **No non-zero setpoint was ever written.**
15:58:32 cable back: **reconnected after 36 attempts, stable (no flapping), 0 kW written and confirmed after 363 failed attempts**; DISCONNECTED -> SAFE_IDLE. 15:59:31 server back: SAFE_IDLE -> CONNECTED.
Recording gap 15:52:06 to 15:58:30 (183 rows with a note); at the first sample after reconnect the BMS was On / Normal, alerts 0x00000000 (**no alert after about 6.5 minutes without a controller**), and our heartbeat echo was stale (39796 against 40184) and caught up within 2 s.
Evidence: audit rows 22:50 to 22:59 UTC; recordings\bms_20261016_224943.csv.

## Test 13E: Event first seen after it expired: PASS in two layers (16:01)
Trying to add an event that started about 30 minutes earlier (15:30 to 15:31) on the Schedule page: **the utility side refused it ("Not added. The start time is in the past.")**; nothing reached the gateway and nothing ran. The gateway's own rejection (an event received after it ended is answered 254, "Rejected, event received after it had expired", and never runs) is covered by tests/test_ieee_responses.py (an event that is "already over" gets [254]; 40 tests in that file pass), because the dashboard will not let us send an expired event to the gateway. No battery power was involved.

## Wrap-up (16:03 to 16:15)
Gateway stopped cleanly at 16:03:22 (Ctrl+C in its tab): final 0 kW written and confirmed ("stopped: battery at Safe Idle (0 kW), confirmed by the BMS"); BMS_READ_ONLY=1 restored; server stopped; no process of ours left running; no BMS connection held. The battery was left On / Normal / idle at 0 kW with the Warning (as the engineer had it).
Test suite: 514 tests. The dashboard tests in tests/test_observability.py assert exact wall-clock ages (a BMS reading "0 s old", or "60 s" exactly). **Root-caused and fixed:** test_dashboard_keeps_requested_verified_and_measured_apart failed every time after the register pacing was added, because its rig runs a connect, a dispatch and a status read back to back (about 100 registers in under a second) and the driver now spreads that over about 2 s (age 1 s, not 0); the rig now runs with register_pacing=False (3 of 3 passes, with a comment saying why). **Not fixed, flagged:** test_dashboard_flags_stale_bms_data_and_shows_bms_status (and, before the pacing existed, the same two tests) failed in 3 of 4 full-suite runs and passed alone and in the last full run: exact-age assertions are racy on a loaded machine. The final full run was entirely green (514 passed).
