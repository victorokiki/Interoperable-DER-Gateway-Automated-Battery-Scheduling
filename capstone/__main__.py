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
import logging
import os
import signal
import sys

from capstone.state import GatewayState
from capstone.modbus_driver import ModbusDriver
from capstone.scheduler import Scheduler
from capstone.ieee_client import IEEEClient
from capstone.audit_log import AuditLog
from capstone.reliability import ReliabilityManager
from capstone.event_bus import EventBus

log = logging.getLogger("capstone.main")


def load_config() -> dict:
    """Build the config dict from environment variables, falling back to
    values that work with the local simulators for easy first-run testing."""
    return {
        "ieee_server_url":       os.environ.get("IEEE_SERVER_URL", "http://localhost:5001"),
        "ieee_poll_interval_sec": int(os.environ.get("IEEE_POLL_INTERVAL_SEC", "5")),
        "offline_timeout_sec":   int(os.environ.get("OFFLINE_TIMEOUT_SEC", "30")),
        "modbus_host":           os.environ.get("MODBUS_HOST", "127.0.0.1"),
        "modbus_port":           int(os.environ.get("MODBUS_PORT", "5020")),
        # NF-04: TLS 1.2+ mutual cert auth for IEEE 2030.5 — all three
        # optional; leave unset to keep talking plain HTTP to the local
        # simulator. See certs/generate_dev_certs.sh for dev certs.
        "tls_client_cert":       os.environ.get("TLS_CLIENT_CERT"),
        "tls_client_key":        os.environ.get("TLS_CLIENT_KEY"),
        "tls_ca_cert":           os.environ.get("TLS_CA_CERT"),
    }


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
    print(f"  IEEE 2030.5 server : {config['ieee_server_url']}")
    print(f"  Poll interval      : {config['ieee_poll_interval_sec']}s")
    print(f"  Offline timeout    : {config['offline_timeout_sec']}s")
    print(f"  Modbus battery     : {config['modbus_host']}:{config['modbus_port']}")
    print(f"  Reliability FSM    : CONNECTED (initial state)")
    print(f"  Audit log          : schedule_store/audit_log.db")
    print("=" * 60)
    print("  Press Ctrl+C to shut down safely.")
    print("=" * 60 + "\n")


async def event_bus_logger(bus: EventBus):
    """NF-05: a minimal subscriber demonstrating the event bus is live —
    logs every state-change / alert notification published to it. A
    future dashboard or ML advisory module would subscribe the same
    way, without the publishers needing any changes."""
    queue = bus.subscribe()
    while True:
        message = await queue.get()
        log.info(f"[event_bus] {message['topic']}: {message['payload']}")


async def run():
    setup_logging()
    config = load_config()
    print_banner(config)

    state = GatewayState()
    audit_log = AuditLog()
    event_bus = EventBus()
    reliability = ReliabilityManager(config, state, audit_log=audit_log, event_bus=event_bus)

    modbus = ModbusDriver(config, state, audit_log=audit_log, event_bus=event_bus)
    await modbus.connect()

    scheduler = Scheduler(config, state, modbus, audit_log=audit_log)
    ieee = IEEEClient(config, state, scheduler, reliability_manager=reliability, audit_log=audit_log)

    tasks = [
        asyncio.create_task(ieee.run(), name="ieee_client"),
        asyncio.create_task(scheduler.run(), name="scheduler"),
        asyncio.create_task(modbus.watchdog(), name="modbus_watchdog"),
        asyncio.create_task(modbus.heartbeat_loop(), name="modbus_heartbeat"),
        asyncio.create_task(modbus.status_poll_loop(), name="modbus_status_poll"),
        asyncio.create_task(event_bus_logger(event_bus), name="event_bus_logger"),
    ]

    stop_event = asyncio.Event()

    def _handle_shutdown_signal():
        log.info("Shutdown signal received — stopping gracefully...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_shutdown_signal)
        except NotImplementedError:
            # add_signal_handler isn't available on some platforms (e.g. Windows)
            signal.signal(sig, lambda *_: _handle_shutdown_signal())

    try:
        await stop_event.wait()
    finally:
        log.info("Cancelling background tasks...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        log.info("Forcing battery to safe IDLE before exit...")
        try:
            await asyncio.wait_for(modbus.safe_idle(), timeout=5)
        except Exception as e:
            log.error(f"Could not confirm safe IDLE on shutdown: {e}")

        log.info("Shutdown complete.")


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        # Fallback for platforms/edge cases where the signal handler above
        # doesn't catch it before asyncio.run() propagates KeyboardInterrupt.
        print("\nShutdown requested. Exiting.")
        sys.exit(0)


if __name__ == "__main__":
    main()