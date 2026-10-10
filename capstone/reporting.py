"""
capstone/reporting.py
What the gateway reports to the utility about its DER (CSIP; IEEE 2030.5 DER
and Metering Mirror function sets), to the links discovery found
(IEEEClient.links); nothing is sent where the utility gives no link.

  DERCapability  the controls the gateway executes and the DER's power
                 ratings (DER_SET_MAX_W etc.: the only ratings configured);
                 PUT once per start, and again if it changes.
  DERSettings    the settings in force; PUT once per start and on change.
  DERStatus      running or not, connection, storage mode, state of charge,
                 local or remote control, from the BMS status the driver
                 read; PUT at the DERList's pollRate (default 900 s) and
                 whenever one of those changes.
  Metering       a MirrorUsagePoint created once (POST to the
                 MirrorUsagePointList), then active and reactive power every
                 5 minutes (CSIP), as the BMS measures them.

BMS readings older than STALE_BMS_SEC are not reported. Not reported at all,
because the BMS does not measure them: voltage, frequency, and the CSIP DER
alarms (over/under voltage and frequency, imbalance, phase rotation, ...),
which belong to the PCS.

Every time sent is on the utility's clock (IEEE 2030.5 Time; see
IEEEClient._sync_time). A report the utility refuses, or that cannot reach
it, is retried with backoff (5 s doubling to 5 min) and audited when it
starts failing and when it goes through again.
"""

import asyncio
import hashlib
import logging
import time
from fractions import Fraction
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import aiohttp

from capstone.ieee_client import _LOOPBACK_HOSTS
from capstone.modbus_driver import OPERATING_STATE_NAMES, STATE_FAULT, STATE_OFF, STATE_ON

log = logging.getLogger("reporting")

NS = "urn:ieee:std:2030.5:ns"
SENT = (200, 201, 204)

# DERControlType bits (IEEE 2030.5) of the controls the gateway executes:
# charge and discharge mode, opModConnect, opModEnergize, opModFixedW, opModTargetW.
MODES = {"charge": 0, "discharge": 1, "opModConnect": 2, "opModEnergize": 3, "opModFixedW": 7, "opModTargetW": 22}
MODES_SUPPORTED = sum(1 << bit for bit in MODES.values())
DER_TYPE = 80                 # DERType 80: other storage system (a flow battery)
SET_GRAD_W = 0                # DERSettings setGradW 0: no ramp-rate limit (the BMS ramps at its own rate)

STATUS_RATE = 900             # IEEE 2030.5 DERList pollRate default: DERStatus at least this often
POST_RATE = 300               # CSIP: monitoring data every 5 minutes
STALE_BMS_SEC = 30            # BMS readings older than this are not reported
RETRY_START_SEC, RETRY_MAX_SEC = 5, 300

# ConnectStatusType bits
CONNECTED, AVAILABLE, OPERATING, FAULT = 0x01, 0x02, 0x04, 0x10
# InverterStatusType for each Endurium operating state (31000): 0 not applicable
# while running normally, 1 off, 3 starting up, 6 shutting down, 7 fault, 8 standby.
INVERTER_STATUS = {0: 0, 1: 1, 2: 8, 3: 3, 4: 0, 5: 6, 6: 6, 7: 6, 8: 3, 9: 6, 10: 7}
OPERATIONAL, OFF = 2, 1                    # OperationalModeStatusType
CHARGING, DISCHARGING, HOLDING = 0, 1, 2   # StorageModeStatusType
LOCAL, REMOTE = 0, 1                       # LocalControlModeStatusType

# MirrorUsagePoint roleFlags: isMirror, isDER, isSubmeter.
ROLE_FLAGS = 0x01 | 0x08 | 0x40
# The readings posted: (name, description, uom). Values in k (powerOfTenMultiplier
# 3), flowDirection 19 ("received from customer"): + = power the DER delivers.
READINGS = (
    ("active_power", "Active power of the battery system (BMS measurement; + = delivered)", 38),
    ("reactive_power", "Reactive power of the battery system (BMS measurement, its sign)", 63),
)


def device_pen(config: dict) -> tuple[int | None, str]:
    """The IANA Private Enterprise Number in the mRIDs this gateway creates
    (IEEE 2030.5 mRIDType: the creating organisation's PEN in the low 32
    bits), and where it came from: DEVICE_PEN; for the local simulator
    only, 0 as a placeholder; otherwise None, and metering is not sent.
    Raises ValueError for a DEVICE_PEN that does not fit 32 bits."""
    pen = config.get("device_pen")
    if pen is not None:
        if not 0 < pen < 1 << 32:
            raise ValueError(f"DEVICE_PEN must be an IANA Private Enterprise Number (1 to {(1 << 32) - 1}), got {pen}")
        return pen, "DEVICE_PEN"
    if (urlsplit(config.get("ieee_server_url") or "").hostname or "").lower() in _LOOPBACK_HOSTS:
        return 0, "placeholder 0, local simulator only"
    return None, "set DEVICE_PEN to the IANA PEN of the organisation responsible for this gateway"


def active_power(tag: str, watts: int) -> str:
    """An IEEE 2030.5 ActivePower element: value (Int16) x 10^multiplier W,
    the smallest multiplier that fits; exact when the watts allow it, else
    the nearest (halves away from zero)."""
    for multiplier in range(10):
        exact = Fraction(watts, 10 ** multiplier)
        value = int(abs(exact) + Fraction(1, 2)) * (1 if exact >= 0 else -1)
        if -32768 <= value <= 32767:
            return f"<{tag}><multiplier>{multiplier}</multiplier><value>{value}</value></{tag}>"
    raise ValueError(f"{watts} W is more than an IEEE 2030.5 ActivePower can hold")


def der_capability_xml(ratings: dict) -> str | None:
    """DERCapability; None without setMaxW (rtgMaxW is required)."""
    if not ratings.get("setMaxW"):
        return None
    parts = [f"<modesSupported>{MODES_SUPPORTED:08X}</modesSupported>"]
    if ratings.get("setMaxChargeRateW"):
        parts.append(active_power("rtgMaxChargeRateW", ratings["setMaxChargeRateW"]))
    if ratings.get("setMaxDischargeRateW"):
        parts.append(active_power("rtgMaxDischargeRateW", ratings["setMaxDischargeRateW"]))
    parts.append(active_power("rtgMaxW", ratings["setMaxW"]))
    parts.append(f"<type>{DER_TYPE}</type>")
    return f'<DERCapability xmlns="{NS}">{"".join(parts)}</DERCapability>'


def der_settings_xml(ratings: dict, updated: int) -> str | None:
    """DERSettings, in force since `updated`; None without setMaxW."""
    if not ratings.get("setMaxW"):
        return None
    parts = [f"<modesEnabled>{MODES_SUPPORTED:08X}</modesEnabled>", f"<setGradW>{SET_GRAD_W}</setGradW>"]
    if ratings.get("setMaxChargeRateW"):
        parts.append(active_power("setMaxChargeRateW", ratings["setMaxChargeRateW"]))
    if ratings.get("setMaxDischargeRateW"):
        parts.append(active_power("setMaxDischargeRateW", ratings["setMaxDischargeRateW"]))
    parts.append(active_power("setMaxW", ratings["setMaxW"]))
    parts.append(f"<updatedTime>{updated}</updatedTime>")
    return f'<DERSettings xmlns="{NS}">{"".join(parts)}</DERSettings>'


def der_status(when: int, code: int, control_mode: str | None, soc_pct, measured_kw) -> tuple[str, tuple]:
    """DERStatus at `when` from the BMS status: operating state `code`
    (31000), control mode, state of charge (%) and measured power (kW, + =
    charge). Returns (XML, the values whose change is reported at once)."""
    running = code == STATE_ON or (code not in OPERATING_STATE_NAMES and measured_kw not in (None, 0))
    connect = (CONNECTED | AVAILABLE | OPERATING if running else
               FAULT if code == STATE_FAULT else 0 if code == STATE_OFF else AVAILABLE)
    inverter = 0 if running else INVERTER_STATUS.get(code, 0)
    local = {"Normal": REMOTE, "User-Controlled": LOCAL}.get(control_mode)
    storage = (None if measured_kw is None else
               CHARGING if measured_kw > 0 else DISCHARGING if measured_kw < 0 else HOLDING)

    def value(tag, v):
        return f"<{tag}><dateTime>{when}</dateTime><value>{v}</value></{tag}>"
    parts = [value("genConnectStatus", f"{connect:02X}"), value("inverterStatus", inverter)]
    if local is not None:
        parts.append(value("localControlModeStatus", local))
    parts.append(value("operationalModeStatus", OPERATIONAL if running else OFF))
    parts.append(f"<readingTime>{when}</readingTime>")
    if soc_pct is not None:
        parts.append(value("stateOfChargeStatus", max(0, min(10_000, round(soc_pct * 100)))))
    if storage is not None:
        parts.append(value("storageModeStatus", storage))
    parts.append(value("storConnectStatus", f"{connect:02X}"))
    xml = f'<DERStatus xmlns="{NS}">{"".join(parts)}</DERStatus>'
    return xml, (connect, inverter, local, running, storage)


def mrid(kind: str, lfdi: str, pen: int) -> str:
    """An mRID for something this gateway creates: IEEE 2030.5 puts the IANA
    PEN of the organisation creating it in the low 32 bits; the other 96
    are unique to it (and stable across restarts, from the LFDI)."""
    unique = hashlib.sha256(f"{kind}:{lfdi}".encode()).hexdigest()[:24]
    return f"{unique}{pen:08x}".upper()


def reading_type_xml(uom: int) -> str:
    return (f"<ReadingType><accumulationBehaviour>12</accumulationBehaviour><commodity>1</commodity>"
            f"<flowDirection>19</flowDirection><kind>37</kind><phase>224</phase>"
            f"<powerOfTenMultiplier>3</powerOfTenMultiplier><uom>{uom}</uom></ReadingType>")


def meter_reading_xml(name: str, description: str, uom: int, value: int, when: int, lfdi: str, pen: int,
                      namespace: bool = True) -> str:
    """A MirrorMeterReading carrying one instantaneous reading."""
    xmlns = f' xmlns="{NS}"' if namespace else ""
    return (f"<MirrorMeterReading{xmlns}><mRID>{mrid('reading:' + name, lfdi, pen)}</mRID>"
            f"<description>{escape(description)}</description>"
            f"<Reading><timePeriod><duration>0</duration><start>{when}</start></timePeriod>"
            f"<value>{value}</value></Reading>{reading_type_xml(uom)}</MirrorMeterReading>")


def mirror_usage_point_xml(values: dict, when: int, lfdi: str, pen: int) -> str:
    """The MirrorUsagePoint the readings go to, created with the first ones."""
    readings = "".join(meter_reading_xml(name, description, uom, values[name], when, lfdi, pen, namespace=False)
                       for name, description, uom in READINGS)
    return (f'<MirrorUsagePoint xmlns="{NS}"><mRID>{mrid("usage point", lfdi, pen)}</mRID>'
            f"<description>DER gateway: battery system power (BMS measurements)</description>"
            f"<roleFlags>{ROLE_FLAGS:04X}</roleFlags><serviceCategoryKind>0</serviceCategoryKind><status>1</status>"
            f"<deviceLFDI>{lfdi}</deviceLFDI>{readings}<postRate>{POST_RATE}</postRate></MirrorUsagePoint>")


class Reporter:
    """Sends the reports above; runs as the supervised task ieee_reporting."""

    def __init__(self, client, state, audit_log=None, pen: int | None = None, pen_source: str = ""):
        self.client = client          # IEEEClient: links, session, the utility's origin, LFDI, ratings
        self.state = state
        self.audit_log = audit_log
        self.pen, self.pen_source = pen, pen_source
        self._sent = {}               # report -> what was last sent, and where
        self._sent_at = {}            # report -> monotonic time it was last sent
        self._retry = {}              # report -> (monotonic time of the next try, delay)
        self._failing = {}            # report -> failure last audited
        self._noted = {}              # topic -> condition last audited
        self._settings_time = None    # DERSettings updatedTime: when these settings were first reported
        self._settings_for = None     # the ratings that time is for
        self.usage_point = None       # the MirrorUsagePoint href, once created

    async def report_loop(self):
        async with self.client._session() as session:
            while True:
                await self.report_once(session)
                await asyncio.sleep(1)

    async def report_once(self, session):
        """Send whatever is due now."""
        links = self.client.links
        if not any((links.capability, links.settings, links.status, links.mirror_usage_points)):
            return                                  # the utility asks for no reports
        if self.state.dispatch_kind == "dc_not_dispatchable":
            self._note("der", "DC-configured BMS: not dispatched by this gateway, so nothing is reported for it")
            return
        await self._report_capability(session, links)
        await self._report_settings(session, links)
        await self._report_status(session, links)
        await self._report_metering(session, links)

    # ── The reports ───────────────────────────────────────────

    async def _report_capability(self, session, links):
        body = der_capability_xml(self.client.der_ratings)
        if body is None:
            if links.capability or links.settings:
                self._note("ratings", "DERCapability and DERSettings not reported: DER_SET_MAX_W is not set "
                                      "(rtgMaxW and setMaxW are required)")
            return
        if links.capability is None or self._sent.get("capability") == (links.capability, body):
            return
        if await self._send(session, "capability", "PUT", links.capability, body, "DERCapability"):
            self._sent["capability"] = (links.capability, body)
            self._audit(f"DERCapability sent to {links.capability}: modes {MODES_SUPPORTED:08X} "
                        f"({', '.join(MODES)}), type {DER_TYPE}", "sent")

    async def _report_settings(self, session, links):
        ratings = self.client.der_ratings
        if self._settings_for != ratings:                       # updatedTime: when these settings took effect
            self._settings_for, self._settings_time = dict(ratings), self._utility_now()
        body = der_settings_xml(ratings, self._settings_time)
        if body is None or links.settings is None or self._sent.get("settings") == (links.settings, body):
            return
        if await self._send(session, "settings", "PUT", links.settings, body, "DERSettings"):
            self._sent["settings"] = (links.settings, body)
            self._audit(f"DERSettings sent to {links.settings}", "sent")

    async def _report_status(self, session, links):
        if links.status is None:
            return
        read_at = self._fresh_reading()
        if read_at is None:
            return self._note("status", f"DERStatus not sent: no BMS reading in the last {STALE_BMS_SEC} s")
        self._note("status", None)
        state = self.state
        xml, changes = der_status(int(read_at + state.clock_offset_sec), state.operating_state_code,
                                  state.control_mode, state.soc_pct, state.measured_kw)
        rate = max(1, links.status_rate or STATUS_RATE)
        unchanged = self._sent.get("status") == (links.status, changes)
        if unchanged and time.monotonic() - self._sent_at.get("status", float("-inf")) < rate:
            return
        if await self._send(session, "status", "PUT", links.status, xml, "DERStatus"):
            if not unchanged:
                connect, inverter, local, running, storage = changes
                self._audit(f"DERStatus sent to {links.status}: {'operational' if running else 'off'}, "
                            f"connect status {connect:02X}, inverter status {inverter}"
                            + (f", storage mode {storage}" if storage is not None else ""), "sent")
            self._sent["status"], self._sent_at["status"] = (links.status, changes), time.monotonic()

    async def _report_metering(self, session, links):
        if links.mirror_usage_points is None:
            return
        lfdi = self.client.lfdi
        if lfdi is None or self.pen is None:
            return self._note("metering", "metering not sent: " + ("no LFDI" if lfdi is None else
                                          f"no IANA PEN for the mRIDs this gateway creates ({self.pen_source})"))
        if self.state.dispatch_kind != "ac_active_power":
            return self._note("metering", "metering not sent: the BMS is not AC-configured, so it measures no "
                                          "AC power")
        read_at = self._fresh_reading()
        if read_at is None or self.state.measured_kw is None or self.state.measured_kvar is None:
            return self._note("metering", f"metering not sent: no BMS power reading in the last "
                                          f"{STALE_BMS_SEC} s")
        self._note("metering", None)
        when = int(read_at + self.state.clock_offset_sec)
        values = {"active_power": -int(self.state.measured_kw), "reactive_power": int(self.state.measured_kvar)}
        if self.usage_point is None:
            body = mirror_usage_point_xml(values, when, lfdi, self.pen)
            location = await self._send(session, "metering", "POST", links.mirror_usage_points, body,
                                        "MirrorUsagePoint", want_location=True)
            if location:
                self.usage_point, self._sent_at["metering"] = location, time.monotonic()
                self._audit(f"MirrorUsagePoint created at {location} (PEN {self.pen}, {self.pen_source}): "
                            f"active and reactive power every {POST_RATE} s", "sent")
            return
        if time.monotonic() - self._sent_at.get("metering", float("-inf")) < POST_RATE:
            return
        for name, description, uom in READINGS:
            body = meter_reading_xml(name, description, uom, values[name], when, lfdi, self.pen)
            if not await self._send(session, "metering", "POST", self.usage_point, body, "MirrorMeterReading"):
                return
        self._sent_at["metering"] = time.monotonic()

    # ── Sending ───────────────────────────────────────────────

    async def _send(self, session, report: str, method: str, href: str, body: str, label: str,
                    want_location: bool = False):
        """PUT or POST one report. Returns True when the utility took it (the
        Location it gave, with want_location), else False; failures back off."""
        now = time.monotonic()
        next_try, delay = self._retry.get(report, (0.0, 0))
        if now < next_try:
            return False
        url = self.client._resource_url(href)
        code = location = None
        if url is None:
            problem = f"{href} is not on the utility server"
        else:
            try:
                async with session.request(method, url, data=body, headers={"Content-Type": "application/sep+xml"},
                                           timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    await resp.read()
                    code, location = resp.status, resp.headers.get("Location")
                problem = (None if code in SENT and (location or not want_location) else
                           f"HTTP {code} without a Location" if code in SENT else f"HTTP {code}")
            except Exception as e:
                problem = f"{type(e).__name__}: {e}".rstrip(": ")
        if problem is None:
            self._retry.pop(report, None)
            recovered = self._failing.pop(report, None)
            if recovered:
                self._audit(f"{label} sent to {href} again (it failed: {recovered})", "sent")
            return location if want_location else True
        if code == 404 and report == "metering" and not want_location:
            self.usage_point = None                     # the utility no longer has it: create it again
        delay = RETRY_START_SEC if not delay else min(delay * 2, RETRY_MAX_SEC)
        self._retry[report] = (now + delay, delay)
        detail = f"{label} to {href}: {problem}"
        if self._failing.get(report) != detail:
            self._failing[report] = detail
            log.warning(f"{detail}; retrying with backoff")
            self._audit(f"{detail}; retrying with backoff", "failed")
        return False

    # ── Helpers ───────────────────────────────────────────────

    def _fresh_reading(self):
        """When the BMS status was last read, if within STALE_BMS_SEC."""
        read_at = self.state.last_status_poll_at
        return read_at if read_at is not None and time.time() - read_at <= STALE_BMS_SEC else None

    def _utility_now(self) -> int:
        return int(time.time() + self.state.clock_offset_sec)

    def _note(self, topic: str, detail: str | None):
        """Audit why something is not reported, once per change (None: it is again)."""
        if self._noted.get(topic) != detail:
            self._noted[topic] = detail
            if detail:
                log.warning(detail)
                self._audit(detail, "not_sent")

    def _audit(self, detail: str, outcome: str):
        if self.audit_log:
            self.audit_log.record(event_type="ieee_reporting", source="reporting", detail=detail, outcome=outcome)
