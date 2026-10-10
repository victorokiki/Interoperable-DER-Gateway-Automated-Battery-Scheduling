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
