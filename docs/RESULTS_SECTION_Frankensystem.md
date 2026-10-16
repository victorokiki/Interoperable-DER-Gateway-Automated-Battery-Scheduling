## 7A. Testing on Invinity's real battery system (16 October 2026)

Everything in section 7 was tested against our own simulators. On 16 October 2026, with Invinity's permission and one of their engineers beside us the whole time, we connected the gateway to Invinity's test system for the first time: a real Endurium battery controller (the BMS) on Invinity's network, reached over Modbus TCP. This section reports what we did, what happened, what the real battery taught us, and what the tests found wrong in our own software. All times are Vancouver time (PDT). The detailed running log is in docs/FRANKENSYSTEM_RESULTS_LOG.md.

### 7A.1 How we kept it safe

- We started read-only. The first checks used the BMS's separate read-only monitoring port, where the BMS itself refuses writes, and the gateway had a switch (BMS_READ_ONLY) that refuses every write before it leaves the computer.
- An Invinity engineer agreed to each new kind of action first, was beside us, and had their own stop control within reach.
- Power was kept small: events of 3 kW, under a ceiling of 5 kW that we added to the gateway (BMS_MAX_POWER_KW), so that a typing mistake (the dashboard's default is 500 kW) could never reach the battery. The only larger step, 25 kW for two minutes during our very first supervised commands, was at the engineer's request. The battery's own limits were 84 kW charge and 189 kW discharge, with operator caps of 50 kW.
- Nothing was trusted on our word alone. A recorder wrote what the BMS itself measured every two seconds, and every command and state change went into the audit log, with the BMS's read-back of each setpoint.
- The battery was in a normal working state: On, charge about 57%, AC-configured, with a Warning that Invinity told us does not stop it working (a warning only reduces performance).

### 7A.2 Results at a glance

The numbers are the test numbers from our test plan (docs/FRANKENSYSTEM_TEST_PLAN.md). Tests 12 and 13A to 13F were added on the day, after the first ten, when Invinity asked for scheduled runs and for what happens when communication is lost. The requirement numbers for tests 1 to 10 are those in the test table we were given; the numbers for tests 12 and 13 are our own mapping.

| # | Test | Requirement | Result | In one line |
|---|---|---|---|---|
| 1 | Connect and identify | F-04 | Pass | Unit ID, AC-configuration and state read correctly; no drops; no writes. |
| 2 | Read-only register survey | F-02 | Pass | Engineer confirmed every value matched their display; write sign confirmed. |
| 3 | Heartbeat, 10 minutes idle | F-04 | Pass | Our echo followed the BMS counter all the time; no alarm. |
| 4 / 4b | Start-up from Standby, and the 5-second wait | F-04 | Pass | Gateway commanded On the same second control was released; waited 5 s before power. |
| 5 | Low-power charge | F-02, F-03, F-04 | Pass | 3 kW written, read back exactly, measured by the BMS, back to 0 on time. |
| 6 | Low-power discharge | F-02, F-04 | Pass | Correct sign; read back exactly; back to 0 on time. |
| 7 / 7b | Replace an event; Force Idle; release; cancel | F-03, F-08 | Pass | Charge replaced by discharge in about 3 s; Force Idle held 0 kW; control resumed. |
| 8 | Utility link lost during an event | F-05, F-07 | Pass | Event ran to its end on its own, then safe idle; connected again on restore. |
| 9 | Battery link lost during an event | F-04, F-05 | Pass, with a finding | Loss logged; the battery kept its last setpoint with no alert (see 7A.4). |
| 10 | Load and audit check | NF-03, NF-06 | Pass after two fixes | Found one audit gap and one one-second burst over the BMS limit; both fixed. |
| 12 | Scheduled start at 12:55 | F-03 | Pass | Started and ended exactly on the scheduled second (repeated at 13:00). |
| 13A | Utility server down when the start passes | F-05, F-07 | Pass | Started and ended exactly on time, on its own. |
| 13B | Battery link down for the whole event | F-04, F-05 | Pass | No power ever flowed; event ended as expired; nothing started late. |
| 13C | Utility server and battery link both down | F-05, F-07 | Pass | Same as 13B; everything recovered by itself. |
| 13D | Battery link returns inside the event | F-04, F-05 | Pass | Charge started 73 s late for the remaining window only; ended on time. |
| 13E | Event first seen after it had expired | F-03 | Pass (two layers) | The utility side refuses it; the gateway's own rejection is covered by unit tests. |
| 13F | Gateway not running when the window passes | F-05 | Pass | On restart the event ended as expired at once; nothing started late. |

### 7A.3 What happened, test by test

### Connecting and reading (tests 1, 2 and 3)

- The gateway found the battery on unit ID 255, detected that it is AC-configured, and read its state (On), control mode (Normal), dispatch modes (already set), operator caps (50 kW each way) and heartbeat. It made no writes. There were no connection drops.
- The engineer read the same values off their own display and confirmed they matched: state, health, charge level, power, the 84 kW and 189 kW limits, control mode, alerts and type. They also confirmed the sign convention for the setpoint register 41009: a negative value is a charge and a positive value is a discharge, which is what the gateway writes.
- Heartbeat: for 10.4 minutes (307 recorded samples) the battery's own counter advanced exactly once a second (622 ticks in 622 s) and our echo in register 41002 followed it with a lag of at most one second. No alert was raised, and the gateway wrote nothing but the echo and a 0 kW setpoint.

### Sending power (tests 5 and 6, and our first commands)

- Our very first supervised commands (10:57 to 11:05) were a 3 kW charge for five minutes and, at the engineer's request, a 25 kW charge for two minutes. Both were written, read back exactly by the BMS, and returned to 0 kW at the end.
- Test 5 (12:02 to 12:03): a 3 kW charge for one minute. The gateway wrote -3 kW, the BMS read it back exactly, the BMS measured 3 kW from the next sample, and it went back to 0 kW at the second the window ended.
- Test 6 (12:05 to 12:06): a 3 kW discharge. The gateway wrote +3 kW, read back exactly; the measured power was negative (discharging) as expected; it returned to 0 kW on time.
- The BMS followed a new setpoint within one to three seconds each time.
- One thing we cannot yet explain: the BMS's measured power read 2 kW for a 3 kW setpoint on most runs (and exactly 3 kW on some). The register holds whole kilowatts, so a real value near 2.5 kW would read 2 or 3. The engineer's screen showed 3 kW on the charge. We report it as a one-kilowatt difference in a whole-number register, not as a failure.

### Replacing, cancelling and Force Idle (tests 7 and 7b)

- A 3 kW charge was replaced by a 3 kW discharge: the utility side cancelled the charge, the discharge took over, and the measured power changed from +2 kW to -2 kW within about three seconds.
- Pressing Clear queue and Force Idle in the middle of the discharge brought the power to 0 kW within a second and cancelled the event (test 7b: the power really stops). 0 kW was held while Force Idle was on, and Release returned control to the schedule: the next event ran at 3 kW and ended on time. All five writes were read back and confirmed.

### Starting the battery (tests 4 and 4b)

- The engineer put the battery in Standby. That switches the BMS into its User-Controlled mode, and in that mode it accepts writes but does not store them. The gateway logged this once, made one attempt that the BMS refused as busy, and then stopped writing: it never claimed a write was confirmed.
- When the engineer released control to Normal, the gateway commanded On within the same second. The BMS went through Starting up and Waiting for Connection and was On 76 seconds later, with no fault and no alert. Its charge and discharge limits were 0 while it was shut down and came back six seconds after On.
- Test 4b: we asked for a 3 kW charge right after the battery was On. The gateway refused to write for 0, 1, 2, 3 and 4 seconds ("settling"), then wrote at 5.2 seconds and the BMS confirmed it. This is the five-second wait the new version of Invinity's specification recommends so that every string can connect to the DC bus.

### Losing the utility server (tests 8 and 13A)

- Test 8: we stopped the utility server in the middle of a four-minute charge. The gateway went CONNECTED, DERATED, AUTONOMOUS, kept charging through the outage, and went to safe idle at the second the event ended. When the server came back it returned to CONNECTED, and the replies that had failed were retried and delivered. Nothing was lost.
- Test 13A: the server was stopped before the event's start time. At 13:18:00 the gateway started the charge on its own, from its stored copy, exactly on time, and ended it exactly on time. The Started reply that failed was retried and delivered with the Completed reply once the server was back.

### Losing the battery link (tests 9, 13B, 13C, 13D and 13F)

- Test 9 (12:25 to 12:30): we unplugged the cable for about 92 seconds during a 3 kW charge. The gateway logged the loss 22 seconds after the first unanswered read. When the cable came back the connection kept dropping and re-forming every 12 seconds for about 90 seconds (the BMS accepted our connection but did not answer it), until it settled about 184 seconds after the cut. The 0 kW at the end of the event could only be written 5 seconds late. The BMS did not stop the charge by itself in that time, and raised no alert.
- Test 13B: the cable was out for six minutes while the battery was idle, and an event (3 kW, 15:12 to 15:13) was due. At 15:12 the event became active but nothing could be written; at 15:13 it ended as expired; the utility only ever saw Received. After the cable came back the gateway reconnected on its first stable try, wrote 0 kW, and started nothing late. No power ever flowed.
- Test 13C: the same with the utility server also down. Same result: expired, no power, everything back to CONNECTED by itself once both links returned.
- Test 13D: the cable returned during the event (15:30 to 15:33). The gateway reconnected, wrote the 3 kW setpoint 73 seconds after the start with 107 seconds left, and ended exactly on time. The event's status is completed, and no time was added.
- Test 13F: the gateway itself was off for about 11 minutes, including an event's whole window. When it was started again the event was ended as expired at once and nothing started late.

### Scheduled starts and events that are already over (tests 12 and 13E)

- Test 12: an event added at 12:50 for 12:55:00 started at 12:55:00 and ended at 12:56:00, to the second, with Started and Completed replies a second later. A second event at 13:00 did the same.
- Test 13E: the utility side refuses to add an event whose start is in the past ("Not added. The start time is in the past."). The gateway's own refusal of an event that arrives already expired (reply code 254) is covered by our unit tests, because the dashboard will not let us send one.

### Load and audit (test 10)

- Over 53 minutes the gateway asked the BMS for 10.2 registers a second on average, against a limit of 50 a second.
- The audit log held every command and every event's full trail. But three of the five BMS state changes in the recording (Shutting Down, Standby and the return to On) had no audit row. We fixed this and added tests (7A.5).
- One second at the start of a scheduled event reached 51 registers, one over the limit. We fixed this too (7A.5).

### 7A.4 What the real battery taught us

These are facts about the battery that the manual does not give, or gives differently. They are worth reporting to Invinity.

| What we saw | Evidence | Why it matters |
|---|---|---|
| The BMS keeps its last setpoint when the controller disappears, and raises no alert. | Test 9: still charging about 184 s after the cut. No alert after 6, 8 or 11 minutes without a controller. | If the link is lost during an event, the battery keeps going until someone or something stops it. Events should be short, and Invinity should say whether this is intended. |
| The BMS keeps a dead control connection for about three minutes. | A 92 s cable cut flapped for 90 s; a six-minute cut recovered immediately. | After a cable cut, the gateway may not be able to talk to the battery for about three minutes. |
| The control mode can only be changed from Invinity's service interface. | The engineer had to release control more than once; register 31036 is read-only. | The gateway cannot take control back itself, by design. |
| The charge and discharge limits go to 0 while the battery shuts down and return after it is On. | Test 4: 0/0 from 12:33:40 until 12:38:43. | Matches the specification; the gateway respects it. |
| The read-only monitoring port (5030) shows zeros for the control registers. | The caps and dispatch modes read as reset on 5030 and as set on 5020. | Control settings must be read on the control port. |
| Measured power is a whole number of kW and read 2 or 3 for a 3 kW setpoint. | Tests 5, 6, 7 and the scheduled runs; the engineer's screen showed 3 kW. | Report accuracy as about 1 kW at these power levels. |
| The BMS answers a write with "busy" (Modbus exception 6) while it is shutting down. | 12:33:30. | The gateway treats it as a refusal and tries again later. |

### 7A.5 What the tests found wrong in our own software, and what we did

Testing on real hardware found problems the simulators could not. All were fixed and have tests. Some were our own mistakes in running the test, and we say so.

| Problem | How it showed up | What we did |
|---|---|---|
| The status poll did not audit BMS state changes (3 of 5 missing). | Test 10, comparing the recording with the audit log. | The poll now audits every state and health change; 3 new tests that fail without the fix. |
| 51 registers requested in one second (the BMS drops anything above 50). | At the start of a scheduled event a heartbeat, a recorder sample and the dispatch landed in the same second (13:18:01). | The gateway now paces itself to at most 45 registers in any second; tests added. The later recording peaked at 44. |
| No wait after On before sending power. | The new specification (Revision 05) recommends five seconds. | Added a five-second hold; shown working on the real battery (test 4b). |
| Our check script called System-Level scope "invalid". | First read of the real battery: scope 0 is a valid value. | Fixed, with a test. |
| Our check script reported zeros from the monitoring port as real settings. | The caps and modes looked reset when the gateway saw them in place. | The check now skips those registers on port 5030 and says why. |
| Two gateways at once, or the utility server not running (our own mistakes). | The BMS takes one control connection, so two gateways knocked each other off; a leftover test process wrote a stale status file. | The launcher now refuses to start a second gateway, or one without the server. |
| A misleading timing in two dashboard tests. | A rig that ran about 100 registers back to back now takes about 2 s because of the pacing. | Fixed for one test; the other still asserts an exact wall-clock age and can flicker on a loaded machine (flagged, not hidden). |

### 7A.6 The limits of this evidence

- One battery, one day, about six hours (first contact 09:37, last test 16:03). Power stayed between 3 kW and 25 kW of a system that can do 84 kW and 189 kW. We did not test full power or long runs.
- The utility was our own simulated utility server, not a real one. The real battery was real; the schedule source was not.
- A Warning was active throughout (Invinity said this is acceptable), and the charge level was about 57%.
- While the cable was out our own recording was blank, so what the BMS itself did during test 9's gap is known only from its state afterwards and from what the engineer reports.
- Two small things we could not explain: a ten-second stall of the gateway at 11:49 with no log message (the BMS raised no alert, so its timeout is longer than ten seconds), and a nine-second CONNECTED, DERATED, CONNECTED blip at 15:31 that matches Windows bringing the network adapter back after the cable was plugged in.
- Dashboard tests: 514 tests pass in the last full run, but two dashboard tests that check exact ages can fail on a busy machine.

Still open, for the engineer: when their screen showed 0 kW in test 9 and whether any alarm appeared; whether keeping the last setpoint on controller loss is intended; the control connection's inactivity timeout; how the bits of the alert register are numbered (Revision 05 numbers its rows from 1 while the register map counts from 0); and why the measured power read 2 kW against 3 kW.

### 7A.7 What this means against our requirements

Requirement numbers are those in our test table. "Met" means shown on this one system under the conditions above.

| Requirement | Tests | Verdict |
|---|---|---|
| F-02 (setpoints and registers correct) | 2, 5, 6 | Met: every written value read back exactly; sign and values confirmed by the engineer. |
| F-03 (scheduling) | 5, 7, 12, 13E | Met: events start and end on the scheduled second, replace and cancel correctly. |
| F-04 (BMS connection, heartbeat, start-up) | 1, 3, 4, 9, 13 | Met: connection, heartbeat and start-up work; battery-link loss is detected and logged. |
| F-05 and F-07 (loss of the utility or the battery link) | 8, 9, 13A to 13F | Met for the gateway's behaviour: it keeps running approved events without the utility, never starts an event late after its window, and recovers by itself. The battery's own behaviour on controller loss is documented in 7A.4 and needs Invinity's answer. |
| F-08 (operator override) | 7, 7b | Met: Force Idle held 0 kW and release returned control. |
| NF-03 (audit trail) | 10 | Met after one fix: every command was recorded; three state changes were not, until fixed. |
| NF-06 (load limit of 50 registers a second) | 10 | Met after one fix: average 10 a second; one one-second burst of 51, now prevented by pacing. |

### 7A.8 Where the evidence is

- docs/FRANKENSYSTEM_RESULTS_LOG.md: the full log of every test with times and numbers.
- docs/FRANKENSYSTEM_TEST_PLAN.md and docs/FRANKENSYSTEM_TEST_DAY.md: the plan and the checklist.
- recordings/*.csv: what the BMS itself measured every two seconds (state, health, charge, measured power, setpoint, alerts, heartbeat, register rate).
- probe_reports/: the read-only checks of the battery. schedule_store/audit_log.db: the audit log.
- capstone/bms_probe.py, capstone/recorder.py, deploy/run_frankensystem.ps1: the tools we built for the day.

### 7A.9 Changes to make elsewhere in this report

- Section 1 and section 7: the test count is now 514 (the last full run was clean).
- Section 8 (risks): add the three new ones: the BMS keeps its last setpoint when the link is lost, the BMS holds a dead connection for about three minutes, and the control mode can only be changed by Invinity's service interface.
- Section 13 (questions for Invinity): mark as answered the register map and the write sign. Add the open questions from 7A.6.
- Section 14 (next steps): a longer run at higher power, a real utility, and Invinity's answers to the open questions.
