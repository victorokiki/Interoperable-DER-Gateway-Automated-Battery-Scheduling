"""
capstone/main.py
Application Entry Point — Interoperable DER Gateway for Automated Battery Scheduling

This wires together the three core components exactly as the architecture
document describes:

    IEEE 2030.5 Client  →  Scheduler (SQLite)  →  Modbus TCP Driver  →  Battery

Configuration is read from environment variables (with sensible defaults
that point at the local simulators), so you can run this straight out of
the box against fake_grid_server.py / fake_battery.py, or point it at a
real utility server / real BMS in production by setting env vars.

USAGE

    # 1. Start the simulators in two other terminals:
    python simulators/fake_grid_server.py
    python simulators/fake_battery.py

    # 2. Run the gateway:
    python -m capstone

    # 3. Point it at real hardware/servers instead, e.g.:
    IEEE_SERVER_URL=https://utility.example.com \\
    MODBUS_HOST=10.0.0.50 MODBUS_PORT=502 \\
    python -m capstone

Press Ctrl+C to shut down gracefully — the battery is forced to a safe
IDLE state before the process exits.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from capstone.state import GatewayState
from capstone.modbus_driver import ModbusDriver
from capstone.scheduler import Scheduler
from capstone.ieee_client import (DER_RATING_SETTINGS, IEEEClient, der_ratings, describe_der_ratings,
                                  device_lfdi, transport_security)
from capstone.audit_log import AuditLog
from capstone.reliability import ReliabilityManager
from capstone.reporting import Reporter, device_pen
from capstone.event_bus import EventBus

log = logging.getLogger("capstone.main")

# Where the gateway publishes a lightweight snapshot of its own state for
# the Grid Operator dashboard's "Battery Status" view (simulators/fake_grid_
# server.py) to read. This keeps the architecture boundary intact — the
# grid-operator side never touches Modbus directly, it only reads what the
# middleware itself chooses to publish, the same way the audit log works.
STATUS_FILE = Path("schedule_store/live_status.json")


class ConfigError(ValueError):
    """The configuration cannot be run safely; the gateway does not start."""


def _whole_number(name: str, default: str) -> int:
    raw = os.environ.get(name, default)
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None


def _yes_no(name: str, default: str) -> bool:
    raw = os.environ.get(name, default).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be 1 or 0, got {raw!r}")


def _optional_whole_number(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from None


def load_config() -> dict:
    """Build the config dict from environment variables, falling back to
    values that work with the local simulators for easy first-run testing.
    Raises ConfigError, before anything connects, for a configuration the
    gateway cannot run safely with."""
    config = {
        "ieee_server_url":       os.environ.get("IEEE_SERVER_URL", "http://localhost:5001"),
        # The utility's DeviceCapability, where discovery of the DER programs starts.
        "ieee_dcap_path":        os.environ.get("IEEE_DCAP_PATH", "/dcap"),
        # Used when the utility gives no pollRate, and between retries after a failed poll.
        "ieee_poll_interval_sec": _whole_number("IEEE_POLL_INTERVAL_SEC", "5"),
        "offline_timeout_sec":   _whole_number("OFFLINE_TIMEOUT_SEC", "30"),
        "modbus_host":           os.environ.get("MODBUS_HOST", "127.0.0.1"),
        "modbus_port":           _whole_number("MODBUS_PORT", "5020"),
        # The BMS's Modbus unit ID. 255 is what Modbus TCP sends to a device
        # addressed directly by its IP; 1 and 0 are tried if it does not answer.
        "modbus_unit_id":        _whole_number("MODBUS_UNIT_ID", "255"),
        # May the gateway start a BMS it finds Off when it takes over (its own
        # start-up, a reconnection, a BMS restart, an operator handing back
        # control)? An Off that appears while it runs is never overridden.
        "bms_start_from_off":    _yes_no("BMS_START_FROM_OFF", "1"),
        # SPC100644 Rev 05 6.2.4 recommends at least 5 s between the BMS reporting On and the first
        # non-zero setpoint, so every string can connect to the DC bus.
        "bms_on_settle_sec":     _whole_number("BMS_ON_SETTLE_SEC", "5"),
        # Look but never touch: the gateway reads the BMS and refuses every write
        # to it. Use it for a first connection to real hardware.
        "bms_read_only":         _yes_no("BMS_READ_ONLY", "0"),
        # NF-04: TLS 1.2+ mutual cert auth for IEEE 2030.5 — all three
        # optional; leave unset to keep talking plain HTTP to the local
        # simulator. See certs/generate_dev_certs.sh for dev certs.
        "tls_client_cert":       os.environ.get("TLS_CLIENT_CERT"),
        "tls_client_key":        os.environ.get("TLS_CLIENT_KEY"),
        "tls_ca_cert":           os.environ.get("TLS_CA_CERT"),
        # Anything weaker than mutual TLS (NF-04) to a host other than this
        # machine is refused unless set (a test network).
        "allow_insecure_transport": os.environ.get("ALLOW_INSECURE_TRANSPORT", "").lower() in ("1", "true", "yes"),
        # The DER's IEEE 2030.5 DERSettings ratings in W (DER_SET_MAX_W,
        # DER_SET_MAX_CHARGE_RATE_W, DER_SET_MAX_DISCHARGE_RATE_W), as the
        # utility has them on file: opModFixedW percentages are converted
        # against them. Unset, opModFixedW events are refused; opModTargetW
        # (watts) still runs.
        **{key: _optional_whole_number(key.upper()) for _, key in DER_RATING_SETTINGS},
        # The LFDI in every DERControlResponse. Derived from TLS_CLIENT_CERT
        # when one is set (IEEE 2030.5); DEVICE_LFDI (40 hex digits) otherwise.
        "device_lfdi":           os.environ.get("DEVICE_LFDI", "").strip() or None,
        # The IANA Private Enterprise Number of the organisation responsible
        # for the gateway: IEEE 2030.5 puts it in every mRID the gateway
        # creates (its metering MirrorUsagePoint). Without it metering is not
        # sent, except to the local simulator.
        "device_pen":            _optional_whole_number("DEVICE_PEN"),
    }
    if config["ieee_poll_interval_sec"] < 1:
        raise ConfigError(f"IEEE_POLL_INTERVAL_SEC must be at least 1, got {config['ieee_poll_interval_sec']}")
    if config["offline_timeout_sec"] < 1:
        raise ConfigError(f"OFFLINE_TIMEOUT_SEC must be at least 1, got {config['offline_timeout_sec']}")
    if config["bms_on_settle_sec"] < 0:
        raise ConfigError(f"BMS_ON_SETTLE_SEC must be 0 or more, got {config['bms_on_settle_sec']}")
    if not 1 <= config["modbus_port"] <= 65535:
        raise ConfigError(f"MODBUS_PORT must be 1-65535, got {config['modbus_port']}")
    if not 0 <= config["modbus_unit_id"] <= 255:
        raise ConfigError(f"MODBUS_UNIT_ID must be 0-255, got {config['modbus_unit_id']}")
    if not config["modbus_host"].strip():
        raise ConfigError("MODBUS_HOST is empty")
    if not config["ieee_dcap_path"].startswith("/"):
        raise ConfigError(f"IEEE_DCAP_PATH must be a path on the utility server, such as /dcap, "
                          f"got {config['ieee_dcap_path']!r}")
    try:
        config["transport"] = transport_security(config)
        config["der_ratings_summary"] = describe_der_ratings(der_ratings(config))
        lfdi, source = device_lfdi(config)
        config["lfdi_summary"] = f"{lfdi} ({source})" if lfdi else f"- ({source})"
        pen, source = device_pen(config)
        config["pen_summary"] = f"{pen} ({source})" if pen is not None else f"- ({source})"
    except ValueError as e:
        raise ConfigError(str(e)) from None
    return config


def setup_logging():
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def print_banner(config: dict):
    print("=" * 60)
    print("  Interoperable DER Gateway — Automated Battery Scheduling")
    print("=" * 60)
    print(f"  IEEE 2030.5 server : {config['ieee_server_url']} (DeviceCapability {config['ieee_dcap_path']})")
    print(f"  Poll interval      : the utility's pollRate, else {config['ieee_poll_interval_sec']}s")
    print(f"  Offline timeout    : {config['offline_timeout_sec']}s")
    print(f"  Modbus battery     : {config['modbus_host']}:{config['modbus_port']} (unit ID "
          f"{config['modbus_unit_id']}, then 1 and 0 if it does not answer)")
    if config["bms_read_only"]:
        print("  BMS access         : READ-ONLY (BMS_READ_ONLY=1) - nothing is written to the BMS")
    print(f"  After On           : no non-zero setpoint for {config['bms_on_settle_sec']} s (SPC100644 Rev 05 6.2.4)")
    print(f"  BMS found Off      : " + ("started when the gateway takes over" if config["bms_start_from_off"]
                                         else "left Off (BMS_START_FROM_OFF=0)"))
    print(f"  Utility transport  : {config['transport']}")
    print(f"  DER ratings        : {config['der_ratings_summary']}")
    print(f"  Response LFDI      : {config['lfdi_summary']}")
    print(f"  Reports            : DER capability, settings and status; metering (mRID PEN {config['pen_summary']})")
    print(f"  Reliability FSM    : CONNECTED (initial state)")
    print(f"  Audit log          : schedule_store/audit_log.db")
    print("=" * 60)
    print("  Press Ctrl+C to shut down safely.")
    print("=" * 60 + "\n")


async def supervise(name: str, factory, audit_log=None, restart_delay: float = 5.0):
    """Run a background loop for the life of the gateway. If it crashes
    (or returns, which none should), log and audit it and start it again,
    so one unexpected error cannot silently end dispatch, polling,
    heartbeats or reconnection."""
    while True:
        try:
            await factory()
            detail, outcome = "exited unexpectedly", "restarting"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception(f"Background task {name} crashed")
            detail, outcome = f"crashed: {type(e).__name__}: {e}", "restarting"
        log.error(f"Background task {name} {detail} — restarting in {restart_delay:.0f}s")
        if audit_log:
            audit_log.record(event_type="task_supervision", source=name, detail=detail, outcome=outcome)
        await asyncio.sleep(restart_delay)


async def event_bus_logger(bus: EventBus):
    """NF-05: a minimal subscriber demonstrating the event bus is live —
    logs every state-change / alert notification published to it. A
    future dashboard or ML advisory module would subscribe the same
    way, without the publishers needing any changes."""
    queue = bus.subscribe()
    while True:
        message = await queue.get()
        log.info(f"[event_bus] {message['topic']}: {message['payload']}")


def status_snapshot(state: GatewayState, reliability: ReliabilityManager) -> dict:
    """What the dashboard shows. It keeps apart what the gateway WANTS
    (`authority`: the dispatch decision and its desired_kw), what the BMS
    CONFIRMED holding (`verified_kw`: read back after the write; None while
    unconfirmed or unknown) and what the BMS MEASURES (`measured_kw`), and
    carries the time of the last successful BMS read (`device_read_at`) so
    device data can be judged stale. `mode` / `power_kw` are the last
    setpoint successfully written, kept for existing readers."""
    return {
        "mode": state.mode.value,
        "power_kw": state.power_kw,
        "last_command_source": state.last_command_source,
        "grid_connected": state.grid_connected,
        "reliability_state": reliability.reliability_state.value,
        "authority": state.authority,
        "verified_kw": state.commanded_kw,
        "measured_kw": state.measured_kw,
        "limits_kw": state.limits_kw,
        "soc_pct": state.soc_pct,
        "operating_state": state.operating_state,
        "operating_status": state.operating_status,
        "control_mode": state.control_mode,
        "dispatch_kind": state.dispatch_kind,
        "bms_heartbeat_stale": state.bms_heartbeat_stale,
        "device_read_at": state.last_status_poll_at,
        "clock_offset_sec": state.clock_offset_sec,
        "published_at": time.time(),
    }


async def publish_status_loop(state: GatewayState, reliability: ReliabilityManager):
    """Writes a small JSON snapshot of the gateway's own state every 2s,
    for the Grid Operator dashboard's Battery Status view to read. The
    dashboard treats the snapshot as stale (rather than trusting it
    forever) based on its own timestamp, the same way gateway
    connectivity is judged by last-poll age — so a killed/crashed
    gateway naturally shows as stale within a couple of seconds rather
    than freezing on the last value indefinitely."""
    STATUS_FILE.parent.mkdir(exist_ok=True)
    while True:
        snapshot = status_snapshot(state, reliability)
        try:
            tmp = STATUS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot))
            tmp.replace(STATUS_FILE)  # atomic — dashboard never reads a half-written file
        except OSError as e:
            log.warning(f"Could not publish status snapshot: {e}")
        await asyncio.sleep(2)


async def run():
    setup_logging()
    config = load_config()
    print_banner(config)

    state = GatewayState()
    audit_log = AuditLog()
    event_bus = EventBus()
    reliability = ReliabilityManager(config, state, audit_log=audit_log, event_bus=event_bus)
    audit_log.record(event_type="gateway", source="gateway", outcome="started", detail=(
        f"started: utility {config['ieee_server_url']} (poll {config['ieee_poll_interval_sec']} s, "
        f"offline timeout {config['offline_timeout_sec']} s, {config['transport']}); "
        f"BMS {config['modbus_host']}:{config['modbus_port']}; reliability state "
        f"{reliability.reliability_state.value} until the first utility poll"))

    modbus = ModbusDriver(config, state, audit_log=audit_log, event_bus=event_bus)
    await modbus.connect()

    scheduler = Scheduler(config, state, modbus, audit_log=audit_log, reliability_manager=reliability)
    ieee = IEEEClient(config, state, scheduler, reliability_manager=reliability, audit_log=audit_log)
    pen, pen_source = device_pen(config)
    reporter = Reporter(ieee, state, audit_log=audit_log, pen=pen, pen_source=pen_source)

    loops = {
        "ieee_client":        ieee.run,
        "ieee_responses":     ieee.response_loop,
        "ieee_reporting":     reporter.report_loop,
        "scheduler":          scheduler.run,
        "modbus_watchdog":    modbus.watchdog,
        "modbus_heartbeat":   modbus.heartbeat_loop,
        "modbus_status_poll": modbus.status_poll_loop,
        "event_bus_logger":   lambda: event_bus_logger(event_bus),
        "status_publisher":   lambda: publish_status_loop(state, reliability),
    }
    tasks = [asyncio.create_task(supervise(name, factory, audit_log), name=name)
             for name, factory in loops.items()]

    stop_event = asyncio.Event()

    def _handle_shutdown_signal():
        log.info("Shutdown signal received — stopping gracefully...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    # SIGBREAK (Windows only) is Ctrl+Break, the console stop a service
    # wrapper can send to one process group; unhandled, it killed the
    # gateway at once and left the battery at its last setpoint.
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _handle_shutdown_signal)
        except NotImplementedError:
            # add_signal_handler isn't available on some platforms (e.g.
            # Windows). A plain signal handler must not touch the event loop
            # directly: hand the stop over thread-safely, which also wakes it.
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(_handle_shutdown_signal))

    try:
        await stop_event.wait()
    finally:
        log.info("Cancelling background tasks...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        log.info("Forcing battery to safe IDLE before exit...")
        try:
            if await asyncio.wait_for(modbus.safe_idle(source="shutdown"), timeout=5):
                result, outcome = "battery at Safe Idle (0 kW), confirmed by the BMS", "stopped"
            else:
                kind, detail = modbus.last_dispatch_error or ("unknown", None)
                result, outcome = f"Safe Idle NOT confirmed ({kind}: {detail})", "stopped_unconfirmed"
        except Exception as e:
            log.error(f"Could not confirm safe IDLE on shutdown: {e}")
            result, outcome = f"Safe Idle NOT confirmed ({type(e).__name__})", "stopped_unconfirmed"
        audit_log.record(event_type="gateway", source="gateway", detail=f"stopped: {result}", outcome=outcome)
        modbus.close()
        # Audit rows are written by a background thread: make sure every one
        # recorded, the stop included, is in the database before exiting.
        if not await asyncio.to_thread(audit_log.flush):
            log.error("Audit log still writing after 10 s at shutdown: the last rows may be lost")

        log.info("Shutdown complete.")


def main():
    try:
        asyncio.run(run())
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        # Fallback for platforms/edge cases where the signal handler above
        # doesn't catch it before asyncio.run() propagates KeyboardInterrupt.
        print("\nShutdown requested. Exiting.")
        sys.exit(0)


if __name__ == "__main__":
    main()