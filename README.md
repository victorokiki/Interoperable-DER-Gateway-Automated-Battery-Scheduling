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

In this public mode the IEEE 2030.5 and `/simulate/*` endpoints answer only to the gateway on the same server, the login is rate-limited, and the demo hint is hidden. Point your own domain at the service with a CNAME record. Data lives in the container's temporary disk, so it resets when the service restarts.
