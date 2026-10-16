# Frankensystem test plan (merged): 10 tests, run in this order

Master list = the 10 tests agreed with the project requirements (IDs F-02 to F-08, NF-03, NF-06), plus three add-ons from our own list.
Power cap for every test: **BMS_MAX_POWER_KW=5**, events of **3 kW** (the engineer may raise it). Mock utility = our `server` window.
Evidence for each test comes from: the audit log (what we sent / what the BMS confirmed), `recordings\bms_*.csv` (what the BMS measured, every 2 s), `probe_reports\`, and the engineer's own screen.

Start of day: two PowerShell windows, `.venv\Scripts\Activate.ps1` in each. Window 1: `.\deploy\run_frankensystem.ps1 server` (leave running). Window 2: gateway. **One gateway at a time** (the BMS control port takes one client).

## Block A: no writes (BMS_READ_ONLY=1)

| # | Test | How | Pass | Evidence | Status |
|---|------|-----|------|----------|--------|
| 1 | Connect and identify | Start the gateway read-only | Stable connection; unit ID resolved (255); AC-configured detected; values plausible | audit rows at start-up | **Done 16 Oct** (re-run for the record) |
| 2 | Read-only register survey | `check` (port 5030) + gateway start-up rows (port 5020). Fill the table below with the engineer reading their display | Every value matches their display; disputed items settled | `probe_reports\`, audit, **engineer's table** | Partly done: **engineer comparison still missing** |

**Survey table (fill with the engineer, number by number):**

| Register | Our reading | Their display | Match? |
|---|---|---|---|
| 31004 charge level (%) | | | |
| 31000 state / 31001 health | | | |
| 31009 AC power (kW) and its sign | | | |
| 31019 / 31021 charge / discharge limit (signs) | | | |
| 31036 control mode | | | |
| 31039 alerts: **is "bit 5" Array Comms Lost or No Active Controller?** (disputed) | | | |
| 41014 / 41016 caps: **does 0 mean "no cap" or "zero allowed"?** (disputed) | | | |
| 41009 sign: negative = charge | confirmed by engineer 16 Oct | | yes |

## Block B: heartbeat (needs BMS_READ_ONLY=0: the heartbeat echo is a write)

| # | Test | How | Pass | Evidence | Status |
|---|------|-----|------|----------|--------|
| 3 | Heartbeat | Gateway idle for 10 minutes, no setpoint | Heartbeat echoed; no "No Active Controller" alarm on the BMS the whole time | recording: `alerts_hex` stays `0x00000000`; engineer's screen | Not done as a timed test |

## Block C: power (cap 5 kW, events 3 kW, 60 s)

| # | Test | How | Pass | Evidence | Status |
|---|------|-----|------|----------|--------|
| 5 | Low-power charge | Run now: charge 3 kW, 60 s | Read-back exact (within 1 LSB); BMS shows charging; returns to 0 kW at the end | audit write + `measured_kw` in the recording + engineer | Done 16 Oct (3 kW, 5 min) **but measured power was not recorded: repeat for the record** |
| 6 | Low-power discharge | Run now: discharge 3 kW, 60 s | Read-back exact; correct sign (+3 written); returns to 0 | same | **Not done** |
| 7 | Supersede and Force Idle | Charge 3 kW for 5 min; after 30 s send a discharge 3 kW with "Replace anything it overlaps"; then **Force Idle**, then **Release** | New event replaces the old; 0 kW held under Force Idle; control resumes on Release | audit `schedule_status` (replaced), `authority` rows; recording | Force Idle done once; supersede and release **not done** |
| 7b (add-on) | Cancel mid-event | Charge 3 kW for 5 min; Force Idle after 30 s | Power really stops, BMS reads 0 kW | recording | Not done |

## Block D: fault injection (3 kW running; the engineer agrees first and is beside you)

| # | Test | How | Pass | Evidence | Status |
|---|------|-----|------|----------|--------|
| 8 | Utility link loss | Charge 3 kW for 3 min. After 30 s stop the **server** window (Ctrl+C). Wait for the event to end, then start the server again | Event continues to its end (state AUTONOMOUS), then safe idle; CONNECTED again on restore | audit `state_transition` rows; recording | Not done (unit-tested only) |
| 9 | BMS link loss | Charge 3 kW. **Unplug the laptop's Ethernet cable**, wait longer than the BMS's comms-lost timeout (ask the engineer for it), plug back in | Loss detected and logged; the BMS's response after its timeout recorded; recovery on reconnect | audit `modbus_link` rows; **engineer reports what the BMS did and when** (our own reads cannot see it while unplugged) | Not done |
| 9b (add-on) | Gateway process killed | Charge 3 kW; end the gateway with Stop-Process (no clean stop) | What the battery does with no controller: recorded | engineer + the first reads after restart | Optional, only if the engineer wants it |

## Block E: state change (only if Invinity agrees)

| # | Test | How | Pass | Evidence | Status |
|---|------|-----|------|----------|--------|
| 4 | Start-up sequence | The engineer puts the battery in **Standby**; the gateway (running) brings it to **On** | Correct Standby then On; ~5 s wait before any power (Rev 05 6.2.4); no fault | audit `bms_command` rows; recording `bms_state` | Not done |
| 11 (add-on) | User-Controlled mode | During an event the engineer switches to User-Controlled | Gateway reports "writes not stored"; no false "confirmed" | audit rows | Optional |

## After the hardware: no BMS needed

| # | Test | How | Pass |
|---|------|-----|------|
| 10 | Load and audit check | From the recording: the `registers_peak_per_s` column. From the audit log: every command and state change present | Peak **<= 50** registers a second; each command and each state change has an audit row |

## Order for today, in one line
**A (1, 2) then B (3) then C (5, 6, 7, 7b) then D (8, 9) then E (4) if agreed, then 10 afterwards.**
Stop and ask the engineer whenever a reading looks odd. Never use another address or port.

## What we need from Invinity before starting
1. Agreement to the heartbeat writes (test 3) and the tests in blocks C, D, E, and the maximum power (we plan 3 kW).
2. The BMS comms-lost timeout (test 9), and what they expect it to do.
3. The answers to the two disputed items in the survey table.
4. Permission to cut our link (test 9), and to have the battery put in Standby (test 4).

---
# Added on the day (16 Oct, after the first 10 tests): scheduled start and missed communication
All 3 kW, 1 minute unless stated, charge. Events are created on the Schedule page ("At a set time", Pacific time) so the gateway holds them before they start.
What the gateway should do (from `capstone/scheduler.py`): an event received before its start runs at its start time from the gateway's own copy; an event whose whole window passes without its setpoint ever being applied ends as **expired** (never started late; the utility only ever sees "Received"); an event still inside its window when the link returns starts late for the remaining time; an event first seen after it ended is **rejected as received after it had expired**.

| # | Test | Steps | Expected | Evidence |
|---|------|-------|----------|----------|
| 12 | Scheduled start at 12:55 | Add a 3 kW charge for 12:55:00 to 12:56:00 at 12:50, then do nothing | Starts at 12:55:00 (within about 2 s), 3 kW confirmed, ends 12:56:00, 0 kW, replies Started and Completed | audit, recording |
| 13A | Utility server down when the start passes | Event 13:00 to 13:01, received by the gateway. Stop the server before 13:00 (about 12:59); restart it after 13:02 | Runs at 13:00 on its own (AUTONOMOUS), ends 13:01, 0 kW; replies queued and delivered after restore | audit, recording |
| 13B | Battery link down for the whole window | Event 13:08 to 13:09. At about 13:04, with the battery idle at 0 kW, unplug the cable; plug back in after 13:11 (the BMS keeps a dead session about 3 minutes) | No power at any time; the event ends **expired**; after reconnect 0 kW is written and nothing starts late | audit (modbus_link, schedule_status expired), recording (no power, blank gap) |
| 13C | Both down for the whole window | Event 13:17 to 13:18. At about 13:13 stop the server and unplug the cable; restore the server first, then the cable, after 13:20 | As 13B; utility sees only "Received" | audit |
| 13D | Battery link returns inside the window | Event 13:26 to 13:29 (3 min). Cable out at about 13:21 (idle), back at about 13:27 | Starts **late** after reconnect for the remaining time, ends 13:29, 0 kW | audit, recording |
| 13E | Event first seen after it expired | Try to add an event whose start was a few minutes ago (the Schedule page may refuse it) | Rejected as received after expiry; nothing runs | audit (RejectedExpired) |

Safety: in 13B and 13C the battery is at 0 kW when the link is cut (it holds 0 kW, as seen in test 9, where it held the last setpoint). 13D charges at 3 kW only after the link is back.
