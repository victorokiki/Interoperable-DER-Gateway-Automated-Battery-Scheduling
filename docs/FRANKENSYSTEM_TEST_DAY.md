# Test day at Invinity: checklist

Print this. Tick each box before moving on. **If anything looks wrong, stop and ask. Never try other addresses or ports.**

## Before the day: ask Invinity, in writing

- [ ] **BMS IP address** (the test network only): ______________________
- [ ] **Control port** (Rev 05 default 5020, one client only): ________   **Read-only monitoring port** (Rev 05 default 5030): ________   **Unit ID** (we default to 255): ________
- [ ] How do I get on their network (cable or Wi-Fi), and do I need a fixed IP? ______________________
- [ ] Is the spec for their firmware **SPC100644 Rev 03**? If not, can I have theirs?
- [ ] **Who is beside me** and **where is the stop button / how do we stop the battery?** ______________________
- [ ] Written OK covering: only the Frankensystem test site, the hosts/ports above, the date and time window, and what I may do (read first, then small commands).
- [ ] Do I need User-Controlled mode switched off for our commands to be stored?
- [ ] What power limits should I stay within? ________ kW

## Before the day: at home

- [ ] Laptop charged, project installed. Open PowerShell in the project folder and run `.venv\Scripts\Activate.ps1`.
- [ ] Run the tests: `python -m pytest tests -q` (all should pass).
- [ ] Copy `deploy\frankensystem.env.example` to `deploy\frankensystem.env`.
- [ ] Practise once against our simulator (see "Practice" below).

## On the day

### 1. Get on their network
- [ ] Connect the way they tell you.
- [ ] Put their BMS address and port into `deploy\frankensystem.env` (`MODBUS_HOST`, `MODBUS_PORT`, `MODBUS_UNIT_ID`). **Leave `BMS_READ_ONLY=1`.**
- [ ] `ping <BMS address>` gets a reply. If not, ask them. Don't guess other addresses.

### 2. Read-only check (nothing is written)
The check uses the BMS's **read-only monitoring port** (`MONITOR_PORT`, 5030 in the Rev 05 spec): the BMS itself refuses writes there, and it does not use up the control port, which takes only **one** client at a time. Ask Invinity to confirm 5030 is open on their system.
```powershell
.\deploy\run_frankensystem.ps1 check
```
- [ ] Every line is PASS or INFO. A WARN or FAIL: read the sentence under it, then ask Invinity.
- [ ] **Compare with their screens, number by number:**
  - Battery charge (%) matches: ______
  - Power (kW) matches, **including the sign** (+/-): ______
  - State (On / Standby / Off) matches: ______
  - Heartbeat is counting up: ______
- [ ] The report was saved in `probe_reports\`. Keep it as evidence.

### 3. Read-only gateway
**Two windows, one gateway.** The BMS control port takes only ONE client, so never run two gateways (the launcher now refuses). Open two PowerShell windows in the project folder and run `.venv\Scripts\Activate.ps1` in each.
```powershell
# window 1 (leave it running)
.\deploy\run_frankensystem.ps1 server
# window 2
.\deploy\run_frankensystem.ps1 gateway
```
- [ ] The banner says **BMS access: READ-ONLY**.
- [ ] The dashboard (`http://localhost:5001/dashboard`) shows the real battery's charge and state.
- [ ] Send nothing yet. Watch for a few minutes. Confirm it never tries a command.

### 4. One small command (only with the Invinity engineer beside you saying yes)
- [ ] They confirm: stop control in reach, limits agreed, the battery is not needed for anything else.
- [ ] Stop the gateway (Ctrl+C). In `deploy\frankensystem.env` change `BMS_READ_ONLY=0`. Start it again and type `YES` when asked.
- [ ] Check the gateway's start-up text: **Power cap** shows the limit the engineer agreed (`BMS_MAX_POWER_KW`, 50 by default, the BMS's own operator cap; lower it for a cautious run and never raise it beyond what they allow) and **Recording** shows a folder. Whatever is typed in Run now, the gateway never writes more than the cap (a 500 kW request is held to the cap).
- [ ] Send one **short, low-power** event (a few kW, a few minutes) from the dashboard.
  - Expect about **5 seconds** before power moves if the battery has only just turned On: the gateway waits on purpose (Rev 05 spec 6.2.4) so every string can connect. The dashboard/audit log shows "settling", then the write.
- [ ] Their monitor shows the power following ours. Note the time: ______
- [ ] Cancel it from the dashboard. Power returns to 0. Note the time: ______

### 5. Finish
- [ ] Set `BMS_READ_ONLY=1` again.
- [ ] Export the audit log and report from the dashboard (Reports page).
- [ ] Save the `probe_reports\` files and any packet capture.
- [ ] Save the `recordings\bms_*.csv` file(s): one row every 2 s with what the BMS itself measured (power, charge, state, health) next to what we commanded. It is the evidence of what the battery did, whatever the screens showed.
- [ ] Thank them, and report anything odd (including the BMS that looked reachable from the internet).

## If something goes wrong

| What you see | What to do |
|---|---|
| `Could not connect` | Check you are on their network, `ping` the address, check the port. Ask Invinity. |
| `Unit ID: no answer` | Ask what unit ID the BMS uses. Put it in `MODBUS_UNIT_ID`. |
| A `FAIL` or `Fault` | Do not send commands. Ask Invinity. |
| Numbers differ from their screen | Stop. It may be scaling or sign. Ask them which is right. |
| Anything unexpected on the battery | Tell the engineer at once and use their stop control. |

## Practice at home (against our simulator)

```powershell
python simulators\fake_battery.py          # in one window
python -m capstone.bms_probe --host 127.0.0.1 --port 5020
```
You should see 9 pass, 0 fail.
