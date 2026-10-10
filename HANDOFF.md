# Handoff Brief — Interoperable DER Gateway (SFU SEE 799 Capstone)

Partner: Invinity Energy Systems. Team: Edward Amponsah, Emmanuel Kwesi Padi,
Victor Okikioluwa Olarewaju. Project root:
`/Applications/XAMPP/xamppfiles/htdocs/capstone_project` (not a git repo).

## What this is
Middleware translating IEEE 2030.5 (XML/HTTPS, DERControl events from a
utility/DERMS) into Modbus TCP writes against an Endurium BMS
(SPC100644 Rev 03), plus a reliability FSM, audit log, event bus, and a
browser-based Grid Operator dashboard.

## Architecture
- `capstone/ieee_client.py` — polls utility server, parses DERControl
  events, sends DERControlResponse acks (F-01), optional mTLS/server-only
  TLS (NF-04).
- `capstone/scheduler.py` — SQLite-backed event store/executor. Handles
  **multiple concurrent/overlapping/back-to-back schedules** (see below).
- `capstone/modbus_driver.py` — writes to BMS registers, sign inversion
  at the hardware boundary, heartbeat, status polling (F-04).
- `capstone/reliability.py` — 5-state FSM: CONNECTED, DERATED,
  DISCONNECTED, AUTONOMOUS, SAFE_IDLE (renamed from DEGRADED per
  Invinity Sprint 1 feedback — verified, see below).
- `capstone/audit_log.py`, `capstone/event_bus.py`, `capstone/state.py`.
- `capstone/__main__.py` — wires it all together, `python -m capstone`.
- `simulators/fake_battery.py` — Modbus TCP simulator (port 5020).
- `simulators/fake_grid_server.py` — Flask utility-server simulator
  AND the Grid Operator dashboard (login, monitoring, command input,
  optional TLS), port 5001.
- `certs/` — dev mTLS certs + generator script.
- `tests/test_capstone.py` — 35 unit tests, all passing.
- `tests/integration/*.py` — 2 end-to-end scripts (real subprocesses,
  real fault injection), both passing.

## Verified this session
1. **DERATED rename** — confirmed live: killed the grid server against a
   running gateway, watched
   `CONNECTED → DERATED → DISCONNECTED → AUTONOMOUS → SAFE_IDLE → CONNECTED`
   in the log, the audit DB, and the dashboard's live status file.
2. **Multiple schedules on one battery** — found and fixed a real bug:
   the scheduler used to force the battery to IDLE whenever ANY event
   ended, even if another event was already active (back-to-back or
   overlapping). Fixed in `scheduler.py` (`_execute_stop` /
   `_active_successor`): an ending event only idles the battery if
   nothing else is active; otherwise control passes to whichever event
   is active, and overlaps resume the earlier event on the later one's
   end. Verified live with a 5-event run (charge → discharge →
   idle-gap → charge → overlapping-discharge → charge → idle), matching
   expected battery output every 2s the whole way, and cross-checked
   against the audit log.
3. **Unsupported DERControl payloads rejected, not silently zeroed** —
   `ieee_client.py:_extract_event` now rejects any DERControl whose
   `DERControlBase` has no active-power setpoint (e.g. only
   `opModMaxLimW`, a percentage cap seen in a partner reference
   screenshot) instead of defaulting it to 0 kW. Logged as a warning +
   `schedule_rejected` audit entry. `opModMaxLimW` is NOT implemented as
   a cap.

   **Update (step 1 of the working answers, 2026-09-28): the setpoint is
   now read the way IEEE 2030.5 defines it.** Two controls are accepted,
   the two CSIP lists for "Set Active Power Mode":
   - `opModFixedW`: a plain number, hundredths of a percent (-10000 to
     10000) of the DER's `setMaxChargeRateW` when negative and of
     `setMaxDischargeRateW` (else `setMaxW`) when positive. The ratings
     come from `DER_SET_MAX_W`, `DER_SET_MAX_CHARGE_RATE_W` and
     `DER_SET_MAX_DISCHARGE_RATE_W` (whole watts, the values the utility
     has on file). Without `DER_SET_MAX_W`, opModFixedW events are
     refused and audited.
   - `opModTargetW`: watts, as `<multiplier>` and `<value>`.

   Both count the power the DER puts out, so on the wire **positive =
   discharge**; the gateway flips the sign at this boundary into its own
   convention (+ = charge). The old form, `<opModFixedW><value>W</value>`
   with positive = charge, was not IEEE 2030.5 and is now refused. The
   simulator sends `opModTargetW` by default, or percentages with
   `SIM_POWER_CONTROL=opModFixedW` (it then needs `DER_SET_MAX_W` too).

   **Update (step 2, 2026-09-29): DERControlResponses follow IEEE 2030.5.**
   - Responses are sent only when an event asks, through its `replyTo`
     and `responseRequired` attributes. Bit 0 asks for "received"; bit 1
     asks for started (the BMS confirmed the setpoint), completed,
     cancelled, superseded, or a refusal: 252 not applicable, 253
     invalid, 254 received after it expired.
   - Each status is sent once, never re-sent on later polls, which
     removes D3. Responses wait in a `responses` table of the schedule
     database, written in the same transaction as the event change, and
     a separate supervised task (`ieee_responses`) POSTs them. It retries
     5xx and network errors with backoff (5 s doubling to 5 min, giving
     up after 24 h) and does not retry other 4xx.
   - Responses go only to the utility server itself. Each carries
     `endDeviceLFDI` (from the TLS client certificate; otherwise
     `DEVICE_LFDI`; for the local simulator, `certs/client.crt`), a
     numeric `status`, `subject` (the mRID) and `createdDateTime`, as
     `application/sep+xml`.
   - `opModConnect` / `opModEnergize` false means 0 kW. An event with any
     other control the gateway cannot execute (e.g. `opModMaxLimW`,
     volt-var) is refused as a whole with 252.
   - The simulator asks for responses like CSIP's example
     (`replyTo="/rsps/1/rsp" responseRequired="03"`) and records them in
     `/status`.
   - On the next start, an existing `schedule.db` gets two columns and the
     `responses` table added; nothing is removed.

   **Update (step 3, 2026-09-30): events are found the IEEE 2030.5 way.**
   - Discovery (CSIP §5.2.3.1) replaces the fixed `/derp/1/derc`. The
     gateway starts at `IEEE_DCAP_PATH` (default `/dcap`) and follows:
     - DeviceCapability's `DERProgramListLink` (programs for all devices);
     - its `EndDeviceListLink` → this device's EndDevice (matched by LFDI,
       or the SFDI derived from it) → its FunctionSetAssignments → their
       program lists.
   - Up to 15 programs are followed, lowest primacy first. Every list is
     read page by page (`?s=…&l=255`) until `all` items are in.
   - Any list that cannot be read fails the whole poll, which counts as a
     link failure, and nothing stored is dropped.
   - Polling follows the utility's `pollRate` (the smallest one given),
     measured from the start of each poll. `IEEE_POLL_INTERVAL_SEC` is used
     when the utility gives none, and for retries after a failed poll.
     For a real utility without a `pollRate`, IEEE 2030.5's default is
     900 s: set `IEEE_POLL_INTERVAL_SEC` accordingly.
   - Overlapping events: the lower program primacy wins, then the newer
     `creationTime` (IEEE 2030.5-2018 §10.2.3.3), then the later start.
     An event superseded by another program's event is answered with 14
     rather than 7.
   - Between events, the DefaultDERControl of the lowest-primacy program
     that asks for a power the gateway can execute applies (authority
     `default_control`). During a utility outage it keeps running, like an
     approved event (AUTONOMOUS). A default the gateway cannot execute as
     a whole (e.g. volt-var) is not applied and is audited; one that could
     not be read this poll keeps the last one read.
   - Not done yet: randomization (randomizeStart / randomizeDuration are
     audited as not applied), subscriptions, time synchronization, and
     what an event that disappears from the list means (U8/UQ10 is still
     open: such an event keeps running).
   - The simulator serves the whole tree, pages lists (`SIM_PAGE_SIZE`),
     can give a `pollRate` (`SIM_POLL_RATE`), and sets a DefaultDERControl
     with `POST /simulate/default`.

   **Update (step 4, 2026-09-30): the Endurium BMS where the ICD is silent
   (working answers E1-E10).** Each is a setting or a runtime check;
   hardware or Invinity's emulator still has to confirm them.
   - E1: On is commanded automatically from Standby. From Off only when
     the gateway takes over (its own start-up, a reconnection, a BMS
     restart, or an operator handing back User-Controlled mode), and only
     if `BMS_START_FROM_OFF` (default 1) allows it.
     - An Off that appears while the gateway runs is left alone: in the
       ICD's state machine (p.16) Off is entered only by an Off command, and
       a restarted BMS resumes its stored state.
     - Outside a take-over: at most 3 automatic On commands an hour, then
       an alarm and nothing more until the next take-over.
     - Never in User-Controlled mode or Fault; never Clear Alerts.
   - E2: 41000 is read before On. If it already holds On while the BMS is
     in Standby, Standby is written first and On 1 s later.
   - E4: a BMS heartbeat stuck for over 5 s while the registers answer
     withdraws the confirmation, writes 0 kW and raises an alarm. Non-zero
     setpoints are refused until it has incremented again for 10 s. The
     echo to 41002 continues throughout.
   - E5: audit rows are written by a background thread, so they never stall
     the event loop that runs the heartbeat. 31039 bit 6 (No Active
     Controller) is audited at connect.
   - E6: besides state 4, a state code missing from the ICD's table
     confirms a setpoint only while the measured power follows it.
   - E7 (AC): 41018-41021 are read at every connect and status poll.
     - Those reading 0 (Invalid) are set to Grid-Feeding, Fixed Active Power
       and Fixed Q (41011 = 0 kVAR first), then read back.
     - Per-Array scope or P-f droop set by someone else is left as it is,
       raises an alarm, and blocks confirmation, 0 kW included.
     - Fixed PF is left as it is and does not block active power.
     - Modes the BMS refuses to be read block confirmation and raise an
       alarm; a read that gets no answer (a failing link) decides nothing.
   - E8: on a DC-configured BMS nothing is written, no dispatch is reported,
     and power events are answered 252.
   - E9: `MODBUS_UNIT_ID` (default 255). At connect 31038 is read with it,
     then 1, then 0 (2 s each); the first that answers is used.
   - E10: the limits are the magnitudes of 31019 / 31021, held within the
     operator caps 41014 / 41016 when those are non-zero (read, never
     written).
   - The battery simulator dispatches only when 41018-41021 select fixed
     active power. They power up Invalid (`SIM_DISPATCH_MODES` sets them).
     It also has test hooks on holding register 41090 (simulator only).

   **Update (step 5, 2026-09-30): TLS profile, the utility's clock, reporting.**
   - TLS: the IEEE 2030.5 cipher suite TLS_ECDHE_ECDSA_WITH_AES_128_CCM_8 is
     offered first (TLS 1.2 minimum; other forward-secret ECDHE suites
     follow). OpenSSL 3 allows CCM_8 only at security level 0, so the
     utility context runs at level 0 with every accepted suite named.
     The suite the server chose is audited (`ieee_transport`). The dev
     certificates are now ECDSA P-256 (regenerate with
     `certs/generate_dev_certs.sh`); the simulator's `TLS_ENABLED` mode
     speaks TLS 1.2 and prefers the IEEE 2030.5 suite.
   - Time: the utility's Time resource (DeviceCapability TimeLink) is read
     at its pollRate (default 900 s). Event windows, "already expired" and
     response times are taken on the utility's clock (offsets under 1 s
     count as none; audited as `ieee_time`).
   - Reporting (`capstone/reporting.py`, supervised task `ieee_reporting`),
     to the DER the utility lists for this EndDevice:
     - DERCapability and DERSettings once per start and on change (the
       ratings are DER_SET_MAX_W etc.; setGradW 0 = no ramp limit);
     - DERStatus at the DERList pollRate and whenever the state changes;
     - metering: a MirrorUsagePoint, then active and reactive power every
       5 minutes. Its mRIDs need the organisation's IANA PEN (`DEVICE_PEN`;
       0 as a placeholder for the local simulator only).
     Not reported, because the BMS does not measure them: voltage,
     frequency and the CSIP DER alarms. BMS data older than 30 s is not
     reported. A DC-configured BMS gets no reports.
   - The grid simulator serves /tm (`SIM_CLOCK_OFFSET_SEC` shifts its
     clock), the DER list (`SIM_DER_POLL_RATE`), the report targets and
     /mup, and shows what it received under `reports` in /status.
4. Full unit + integration suites re-run and passing after all the
   above changes (35 unit tests, 2 integration scripts).

## Explicitly out of scope / not done
- **Multiple DERs (several separate batteries)** — deliberately
  deferred. Current design is single-DER: one poll loop, one Modbus
  connection. Only revisit if Invinity requests it. Would need a
  per-device poll loop + Modbus driver + a device list in config.
- **F-06** — not started.
- **ML autonomous-fallback** (original Sprint 1 proposal concept) —
  descoped; AUTONOMOUS state here means "keep running the last
  approved schedule," not ML-generated dispatch.
- JSON DERControl payloads — not parsed; only XML (matches the actual
  IEEE 2030.5 standard, which is XML/EXI, not XML/JSON).

## Running the demo
```bash
source .venv/bin/activate
python simulators/fake_battery.py &        # port 5020
python simulators/fake_grid_server.py &    # port 5001 (dashboard + utility API)
python -m capstone &                       # the gateway
```
Dashboard: `http://localhost:5001/dashboard`, login `operator` /
`invinity2026` (demo credentials, not for real deployment).
Env vars: `TLS_ENABLED=1` (mutual TLS to gateway), `DASHBOARD_TLS=1`
(server-only HTTPS on the dashboard), `IEEE_POLL_INTERVAL_SEC`,
`OFFLINE_TIMEOUT_SEC`, `DER_SET_MAX_W` / `DER_SET_MAX_CHARGE_RATE_W` /
`DER_SET_MAX_DISCHARGE_RATE_W` (the DER ratings opModFixedW percentages
refer to), `DEVICE_LFDI` (the response LFDI when no client certificate
is configured), `IEEE_DCAP_PATH` (the utility's DeviceCapability,
default `/dcap`), `MODBUS_UNIT_ID` (default 255), `BMS_START_FROM_OFF`
(default 1), `DEVICE_PEN` (IANA PEN for the metering mRIDs),
`SIM_POWER_CONTROL` (simulator: `opModTargetW` or `opModFixedW`),
`SIM_POLL_RATE`, `SIM_PAGE_SIZE`, `SIM_DEVICE_LFDI`, `SIM_DISPATCH_MODES`,
`SIM_CLOCK_OFFSET_SEC`, `SIM_DER_POLL_RATE` (simulators) — see
`capstone/__main__.py` for the full list.

Tests: `python -m pytest tests/test_capstone.py -q` and
`python tests/integration/run_integration_test.py` /
`run_integration_test_modbus_fault.py`.

## Deliverables already produced this project
- `~/Downloads/Sprint2_Presentation_Team_Invinity.pptx` — the team's
  slide deck (edited).
- `~/Desktop/capstone document/MAIN WORK/Sprint_2 Report - UPDATED (TEAM INVINITY).docx`
  — combined report of what was submitted vs. what's actually built.
  Does NOT yet mention the multi-schedule bugfix or the unsupported-
  control rejection from this session — worth adding before final
  submission.

## Open items for whoever picks this up
- No public tunnel is currently running (the last `cloudflared` binary
  used was in a session scratchpad and is gone). Re-download if a
  public demo link is needed again.
- Update the Sprint 2 report with the two fixes above.
- Consider adding `opModMaxLimW` as a power cap only if Invinity
  actually asks for percentage-based curtailment, not fixed setpoints.
