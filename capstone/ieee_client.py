"""
capstone/ieee_client.py
IEEE 2030.5 (SEP 2.0) Client
"""

import asyncio
import logging
import ssl
import xml.etree.ElementTree as ET
import aiohttp

log = logging.getLogger("ieee_client")


def build_mtls_context(config: dict) -> ssl.SSLContext | None:
    """Build a client-side TLS context for NF-04 ("IEEE 2030.5
    communication SHALL use TLS 1.2 or higher with mutual certificate
    authentication"), if the config provides all three cert paths.
    Returns None (plain HTTP/default-TLS behaviour) if TLS isn't
    configured — this keeps the local simulator workflow (plain HTTP)
    working unchanged when these settings are simply absent."""
    cert = config.get("tls_client_cert")
    key  = config.get("tls_client_key")
    ca   = config.get("tls_ca_cert")
    if not (cert and key and ca):
        return None

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cafile=ca)      # trust the utility's CA (verify server)
    context.load_cert_chain(certfile=cert, keyfile=key)  # present our own cert (mutual auth)
    return context


class IEEEClient:
    def __init__(self, config: dict, state, scheduler, reliability_manager=None, audit_log=None):
        self.url       = config["ieee_server_url"]
        self.interval  = config["ieee_poll_interval_sec"]
        self.timeout   = config["offline_timeout_sec"]
        self.state     = state
        self.scheduler = scheduler
        self.reliability_manager = reliability_manager  # optional — F-05 FSM
        self.audit_log = audit_log                       # optional — NF-03
        self._ssl_context = build_mtls_context(config)    # optional — NF-04 mutual TLS

    async def run(self):
        log.info(f"IEEE client polling {self.url} every {self.interval}s")
        if self._ssl_context:
            log.info("TLS 1.2+ mutual certificate authentication ENABLED for IEEE 2030.5 communication")
        connector = aiohttp.TCPConnector(ssl=self._ssl_context) if self._ssl_context else None
        async with aiohttp.ClientSession(connector=connector) as session:
            while True:
                await self._poll(session)
                await asyncio.sleep(self.interval)

    async def _poll(self, session: aiohttp.ClientSession):
        endpoint = f"{self.url}/derp/1/derc"
        try:
            async with session.get(endpoint, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    xml_text = await resp.text()
                    self.state.mark_grid_contact()
                    self._record_poll_outcome(success=True)
                    dispatched = self._parse_and_dispatch(xml_text)
                    for event in dispatched:
                        await self._send_der_control_response(session, event)
                else:
                    log.warning(f"IEEE server returned HTTP {resp.status}")
                    self._record_poll_outcome(success=False, detail=f"HTTP {resp.status}")
        except asyncio.TimeoutError:
            log.error("IEEE server timed out")
            self.state.mark_grid_lost()
            self._record_poll_outcome(success=False, detail="timeout")
        except aiohttp.ClientError as e:
            # aiohttp reuses a persistent connection across polls. A NEW
            # connection failure raises ClientConnectorError, but a server
            # dying mid-session (severing an already-open connection)
            # raises ServerDisconnectedError / ClientOSError instead —
            # both are subclasses of ClientError, which is what we need
            # to catch here to cover both cases.
            log.error(f"Cannot reach IEEE server — grid connection lost ({type(e).__name__})")
            self.state.mark_grid_lost()
            self._record_poll_outcome(success=False, detail=type(e).__name__)
        except Exception as e:
            # Last-resort catch: an unexpected exception here must never
            # silently kill the polling loop — that would stop ALL future
            # polling forever with no visible error, which is exactly
            # what was happening before this fix.
            log.error(f"Unexpected error while polling IEEE server: {e}")
            self.state.mark_grid_lost()
            self._record_poll_outcome(success=False, detail=str(e))

    def _record_poll_outcome(self, success: bool, detail: str = ""):
        """Feed the poll result into the F-05 reliability FSM and the
        NF-03 audit log — both optional, so this is a no-op if either
        wasn't wired in (e.g. in unit tests)."""
        if self.reliability_manager:
            if success:
                self.reliability_manager.record_poll_success()
            else:
                self.reliability_manager.record_poll_failure()
                has_valid_schedule = self.scheduler.has_valid_schedule() \
                    if hasattr(self.scheduler, "has_valid_schedule") else False
                self.reliability_manager.evaluate_with_schedule_status(has_valid_schedule)
        if self.audit_log and not success:
            self.audit_log.record(
                event_type="grid_poll",
                source="ieee_client",
                detail=detail,
                outcome="failed",
            )

    def _parse_and_dispatch(self, xml_text: str) -> list:
        """Parse and hand off every DERControl event found. Returns the
        list of successfully-dispatched events so the caller can send
        DERControlResponse acknowledgements (F-01) — kept as a plain
        return value rather than doing the (async) acknowledgement send
        in here, so this method stays synchronous and independently
        unit-testable."""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            log.error(f"XML parse error: {e}")
            return []

        controls = root.findall("{*}DERControl")
        if not controls:
            log.debug("No DERControl events in response.")
            return []

        dispatched = []
        for ctrl in controls:
            event = self._extract_event(ctrl)
            if event:
                log.info(f"New DERControl event: {event}")
                self.scheduler.add_event(event)
                dispatched.append(event)
        return dispatched

    async def _send_der_control_response(self, session: aiohttp.ClientSession, event: dict):
        """Send a DERControlResponse acknowledgement for a received
        event (F-01: "SHALL send DERControlResponse acknowledgements
        within 30s of event posting"). Sent immediately after dispatch,
        well within that window. A failure to acknowledge is logged but
        never allowed to break polling — a missed ack is recoverable
        (the server will just re-serve the event on the next poll),
        unlike a crashed poll loop."""
        endpoint = f"{self.url}/derp/1/derc/{event['mrid']}/derresponse"
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<DERControlResponse xmlns="urn:ieee:std:2030.5:ns">'
            f"<mRID>{event['mrid']}</mRID>"
            "<status>EventReceived</status>"
            "</DERControlResponse>"
        )
        try:
            async with session.post(
                endpoint, data=body,
                headers={"Content-Type": "application/xml"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status not in (200, 201, 204):
                    log.warning(f"DERControlResponse for {event['mrid']} rejected: HTTP {resp.status}")
                else:
                    log.info(f"DERControlResponse sent for {event['mrid']}")
        except Exception as e:
            log.warning(f"Failed to send DERControlResponse for {event['mrid']}: {e}")

    def _extract_event(self, ctrl_elem) -> dict | None:
        try:
            mrid     = ctrl_elem.findtext("{*}mRID", default="unknown")
            start_ts = int(ctrl_elem.findtext("{*}interval/{*}start", default="0"))
            duration = int(ctrl_elem.findtext("{*}interval/{*}duration", default="0"))
            power_w  = int(ctrl_elem.findtext("{*}DERControlBase/{*}opModFixedW/{*}value", default="0"))
            mult     = int(ctrl_elem.findtext("{*}DERControlBase/{*}opModFixedW/{*}multiplier", default="0"))

            power_kw = (power_w * (10 ** mult)) / 1000.0

            return {
                "mrid":     mrid,
                "start":    start_ts,
                "end":      start_ts + duration,
                "power_kw": power_kw,
                "source":   "ieee2030.5"
            }
        except (ValueError, TypeError) as e:
            log.error(f"Failed to parse DERControl element: {e}")
            return None