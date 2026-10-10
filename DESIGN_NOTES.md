# Design notes: partner review answers

## 1. Control authority and autonomous operation

The gateway does not generate schedules. There is no ML in the dispatch path.
Schedules come from the utility; the operator can only override them to idle.

Which source wins (`capstone/scheduler.py`, highest priority first). The same
order applies in normal operation, during a communication loss and on recovery:

1. **Operator Force Idle**: persisted, survives restarts.
2. **Approved utility schedule** that is active now. Between several events:
   higher-priority program first, then the newest.
3. **Utility default control** (DefaultDERControl), between events.
4. **Safe idle** (0 kW) when nothing above applies.

Communication loss: keep executing a valid approved schedule until it ends;
go to safe idle if there is none. When contact returns, the utility's current
schedule takes over again.

ML, if added later, would be advisory only: an extra input to the reliability
state machine's disconnected sub-state decision (`capstone/reliability.py`),
not a replacement for the order above.

## 2. Reliability states

CONNECTED, DERATED, DISCONNECTED, AUTONOMOUS, SAFE_IDLE. DERATED replaces the
earlier DEGRADED name, per partner feedback. AUTONOMOUS means "still executing
an already-approved schedule", not "generating one".

## 3. Software choices

- **Database:** SQLite for the capstone (no server to run, enough for the
  data volume). Production path: PostgreSQL + TimescaleDB with Alembic
  migrations. Not implemented.
- **Python:** 3.14 (`requires-python = ">=3.14"`). The whole test suite is
  run on 3.14.7; older versions are not tested.
- **Licences:** checked 2026-10-08. Every installed library is permissive
  (MIT, BSD, Apache-2.0, or the PSF licence for aiohappyeyeballs). No GPL or
  other copyleft libraries are used.
