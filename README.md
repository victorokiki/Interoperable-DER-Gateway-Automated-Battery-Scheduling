# Interoperable-DER-Gateway-Automated-Battery-Scheduling

Battery gateway for IEEE 2030.5 (CSIP) utility events, with a Modbus battery driver, a scheduler, an audit log and an operator dashboard.

## Quick start (Windows)

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e . -r dev-requirements.txt
```

The dev TLS certificates and keys are not stored in the repository. Generate them once (needs `openssl`; use Git Bash):

```bash
cd certs && ./generate_dev_certs.sh
```

## Run the demo

Start the three parts from the `.demo` folder (in VS Code: *Run Task > Demo: start all*):

| Part | Command | Port |
|---|---|---|
| Battery simulator | `python simulators/fake_battery.py` | 5020 |
| Utility simulator and dashboard | `python simulators/fake_grid_server.py` | 5001 |
| Gateway | `python -m capstone` (with `IEEE_SERVER_URL=http://127.0.0.1:5001`) | |

Open http://127.0.0.1:5001/dashboard. The demo login is `operator` / `invinity2026` (local demo only).

The dashboard is in `simulators/dashboard_pages.py` and `simulators/fake_grid_server.py`: Overview, Schedule (Pacific-time calendar, queue and CSV import), Live battery, Utility events, Activity log, Alerts & failures and Reports, with a light/dark switch.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest -q
```

## Put it online

`Dockerfile` and `deploy/start.py` run the battery simulator, the operator dashboard and the gateway together on one server. Only the dashboard's port is exposed.

Set these environment variables on the host (for example a Render web service built from this repository's `Dockerfile`):

| Variable | Value |
|---|---|
| `DASHBOARD_PASSWORD` | a new password, at least 12 characters (the demo password is published here, so the server refuses to start with it) |
| `DASHBOARD_SECRET_KEY` | any long random string |
| `DASHBOARD_USERNAME` | optional, default `operator` |

In this public mode the IEEE 2030.5 and `/simulate/*` endpoints answer only to the gateway on the same server, the login is rate-limited, and the demo hint is hidden. The `render.yaml` Blueprint sets all of this up: in Render choose *New > Blueprint* and pick this repository. Point your own domain at the service with a CNAME record. Data lives in the container's temporary disk, so it resets when the service restarts.

## View-only accounts (sign-up)

Visitors can create their own account on the login page (*Create a view-only account*). Such an account can look at every page but cannot send commands, schedule, cancel or use Force Idle; only the operator login (`DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD`) can.

Accounts are kept in your own Supabase project so they survive restarts (the host's disk does not):

1. Create a free project at supabase.com and run `supabase/viewer_accounts.sql` in its SQL Editor.
2. On the server set `SUPABASE_URL` (Project Settings > API > Project URL) and `SUPABASE_SECRET_KEY` (Project Settings > API Keys > the secret key, which stays on the server).

Without those two variables sign-up is switched off. Only a salted scrypt hash of each password is stored, sign-up is rate-limited per address and per day, and logins are limited to 5 failures per 15 minutes per address.

## Testing against a real battery (Invinity's Frankensystem)

Start read-only. `python -m capstone.bms_probe --host <BMS address>` reads the battery and prints a plain-English
PASS / WARN / FAIL for each register; it never writes. `BMS_READ_ONLY=1` makes the gateway itself refuse every
write to the BMS. Settings: `deploy/frankensystem.env.example` (copy to `deploy/frankensystem.env`), launcher:
`deploy/run_frankensystem.ps1 check|gateway`, and a printable checklist: `docs/FRANKENSYSTEM_TEST_DAY.md`.
