"""
deploy/start.py
Runs the whole demo on one server: the battery simulator, the utility simulator with the
operator dashboard, and the gateway. Used by the Dockerfile; also works on a developer's PC.

Only the dashboard's port ($PORT) is meant to be reachable from outside. The battery
(Modbus, port 5020 by default) and the gateway talk to each other over the server's own network only.

    PORT                   the dashboard's port (the host sets it; default 10000)
    DASHBOARD_PASSWORD     required: a new password of at least 12 characters
    DASHBOARD_SECRET_KEY   required: any long random string (keeps logins across restarts)
    DASHBOARD_USERNAME     default: operator
"""

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = Path(os.environ.get("RUNTIME_DIR", ROOT / "runtime"))
PORT = int(os.environ.get("PORT", "10000"))
BATTERY_PORT = int(os.environ.get("BATTERY_PORT", "5020"))
SECRETS = ("DASHBOARD_PASSWORD", "DASHBOARD_SECRET_KEY", "DASHBOARD_USERNAME")
children: dict = {}
stopping = False


def log(message: str) -> None:
    print(f"[start] {message}", flush=True)


def base_env() -> dict:
    env = dict(os.environ)
    env.update(PYTHONUNBUFFERED="1", PYTHONPATH=str(ROOT))
    return env


def without_secrets(env: dict) -> dict:
    return {k: v for k, v in env.items() if k not in SECRETS}


def commands() -> dict:
    grid = base_env()
    grid.update(PUBLIC_MODE=os.environ.get("PUBLIC_MODE", "1"), DASHBOARD_HOST=os.environ.get("DASHBOARD_HOST", "0.0.0.0"),
                DASHBOARD_PORT=str(PORT), SIM_DER_POLL_RATE=os.environ.get("SIM_DER_POLL_RATE", "30"))
    gateway = without_secrets(base_env())
    gateway.update(IEEE_SERVER_URL=f"http://127.0.0.1:{PORT}", DER_SET_MAX_W="1000000",
                   DER_SET_MAX_CHARGE_RATE_W="1000000", DER_SET_MAX_DISCHARGE_RATE_W="1000000")
    battery = without_secrets(base_env())
    battery.update(BATTERY_HOST="127.0.0.1", BATTERY_PORT=str(BATTERY_PORT))      # never reachable from outside the server
    gateway.update(MODBUS_HOST="127.0.0.1", MODBUS_PORT=str(BATTERY_PORT))
    return {
        "battery": ([sys.executable, str(ROOT / "simulators" / "fake_battery.py")], battery),
        "dashboard": ([sys.executable, str(ROOT / "simulators" / "fake_grid_server.py")], grid),
        "gateway": ([sys.executable, "-m", "capstone"], gateway),
    }


def start(name: str) -> None:
    argv, env = commands()[name]
    children[name] = subprocess.Popen(argv, cwd=RUNTIME, env=env)
    log(f"started {name} (pid {children[name].pid})")


def wait_for(check, what: str, seconds: int) -> None:
    end = time.time() + seconds
    while time.time() < end:
        try:
            if check():
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise SystemExit(f"{what} did not come up within {seconds} s")


def battery_up() -> bool:
    with socket.create_connection(("127.0.0.1", BATTERY_PORT), timeout=1):
        return True


def dashboard_up() -> bool:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/login", timeout=2) as response:
        return response.status == 200


def stop_all(*_) -> None:
    global stopping
    stopping = True
    for name in ("gateway", "dashboard", "battery"):        # gateway first: it parks the battery at 0 kW on exit
        process = children.get(name)
        if process and process.poll() is None:
            process.terminate()
    deadline = time.time() + 15
    for process in children.values():
        try:
            process.wait(timeout=max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)
    start("battery")
    wait_for(battery_up, "the battery simulator", 30)
    start("dashboard")
    wait_for(dashboard_up, "the dashboard", 60)
    start("gateway")
    log(f"all three parts are running; dashboard on port {PORT}")
    last_restart: dict = {}
    while not stopping:
        for name, process in list(children.items()):
            if process.poll() is not None and not stopping:
                if name == "dashboard" and process.returncode not in (0, None):
                    log("the dashboard stopped with an error; shutting down so the host restarts the service")
                    stop_all()
                    return process.returncode or 1
                if time.time() - last_restart.get(name, 0) > 5:
                    log(f"{name} stopped (exit {process.returncode}); restarting")
                    last_restart[name] = time.time()
                    start(name)
        time.sleep(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
