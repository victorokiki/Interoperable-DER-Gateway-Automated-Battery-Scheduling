"""
capstone/ieee_client.py
IEEE 2030.5 (SEP 2.0) Client
"""

import asyncio
import contextlib
import logging
import re
import ssl
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.sax.saxutils import escape as xml_escape

import aiohttp

from capstone import responses
from capstone.modbus_driver import INT32_MAX

log = logging.getLogger("ieee_client")

# Bound on the power-of-ten multiplier, so the exact scaling never computes
# an enormous 10**multiplier: parsing runs on the event loop, and a corrupt
# multiplier such as 30000000 takes ~30 s to evaluate, stalling polling,
# dispatch and the BMS heartbeat. Any non-zero value with a multiplier
# above +12 is outside the int32 kW setpoint range anyway.
MAX_MULTIPLIER = 100

# IEEE 2030.5 SignedPerCent: hundredths of a percent, -10000..10000 (10000 = 100 %).
PERCENT_SCALE = 10_000

# The DERSettings power ratings an opModFixedW percentage refers to, with
# their config keys (upper-cased, the environment variables). Whole watts:
# the values the utility has on file for this DER.
DER_RATING_SETTINGS = (
    ("setMaxW", "der_set_max_w"),
    ("setMaxChargeRateW", "der_set_max_charge_rate_w"),
    ("setMaxDischargeRateW", "der_set_max_discharge_rate_w"),
)

# DERControlBase elements the gateway executes. Any other control in an
# event (volt-var, frequency-watt, a power cap, ...) makes it refuse the
# whole event, "not applicable" (252): running the rest would pass the
# event off as done. rampTms is accepted but not applied: the BMS ramps
# at its own rate.
EXECUTABLE_CONTROLS = {"opModFixedW", "opModTargetW", "opModConnect", "opModEnergize", "rampTms"}

# DERControlResponse delivery: responses per batch, and POSTs in flight at once.
RESPONSE_BATCH = 100
RESPONSE_CONCURRENCY = 4

# The development client certificate, whose LFDI identifies the gateway to
# the local simulator when no certificate or DEVICE_LFDI is configured.
DEV_CERTIFICATE = Path(__file__).resolve().parents[1] / "certs" / "client.crt"
_LFDI = re.compile(r"[0-9A-Fa-f]{40}")

# Discovery (CSIP §5.2.3.1: "SHALL traverse all these links and lists").
PAGE_LIMIT = 255          # items asked for per list page (the `l` query parameter)
MAX_PAGES = 1000          # per list: a server whose `all` never matches its pages cannot hold a poll forever
MAX_PROGRAMS = 15         # CSIP §5.2.3: up to 15 DERPrograms per DER
POLL_RATE_BOUNDS = (1, 86_400)
NO_PRIMACY = 256          # a program without primacy ranks below every program with one (0-255)
TIME_POLL_RATE = 900      # IEEE 2030.5 Time pollRate default: how often the utility's clock is read
CLOCK_DEADBAND_SEC = 1.0  # the utility's currentTime is in whole seconds: smaller offsets are noise

# TLS toward the utility. IEEE 2030.5 (TLS 1.2) makes the cipher suite
# TLS_ECDHE_ECDSA_WITH_AES_128_CCM_8 mandatory to support: it is offered
# first. OpenSSL 3 allows CCM_8 suites only at security level 0, so this
# context runs at level 0 and names every suite it accepts: the rest are the
# ones Python's defaults allow (forward-secret ECDHE with AES-GCM, ChaCha20
# or AES, no SHA-1, no DHE), never the weak suites level 0 would add.
IEEE_2030_5_SUITE = "ECDHE-ECDSA-AES128-CCM8"
UTILITY_CIPHERS = (f"@SECLEVEL=0:{IEEE_2030_5_SUITE}:ECDH+AESGCM:ECDH+CHACHA20:ECDH+AES"
                   f":!aNULL:!eNULL:!aDSS:!SHA1")


class _LinkFailure(Exception):
    """The utility could not be read; the poll counts as a link failure."""


class _Refused(Exception):
    """A control the gateway will not execute (outcome "unsupported") or a
    malformed one ("invalid"); kind "no_setpoint" when it asks for no power."""

    def __init__(self, outcome: str, detail: str, kind: str = ""):
        super().__init__(detail)
        self.outcome, self.kind = outcome, kind


@dataclass
class Program:
    """A DERProgram as read in one poll."""
    href: str
    primacy: int              # lower wins; NO_PRIMACY when the server gave none
    controls: list            # its DERControl elements
    default: object           # its DefaultDERControl element, None if it has none, or KEEP


KEEP = object()               # the DefaultDERControl could not be read this time: keep the stored one


@dataclass
class ReportLinks:
    """Where this device's reports go, as discovery found them (None: the
    utility gives no such link)."""
    time: str | None = None                   # DeviceCapability TimeLink
    mirror_usage_points: str | None = None    # DeviceCapability MirrorUsagePointListLink
    der: str | None = None                    # this EndDevice's DER (the first of its DERList)
    capability: str | None = None             # that DER's DERCapabilityLink, and so on
    settings: str | None = None
    status: str | None = None
    availability: str | None = None
    status_rate: int | None = None            # its DERList pollRate: how often DERStatus is sent

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _is_utc_date(unix_ts: int) -> bool:
    """Can this Unix time be stored, scheduled and audited as a UTC date
    (years 1-9999)?"""
    try:
        _EPOCH + timedelta(seconds=unix_ts)
    except OverflowError:
        return False
    return True


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def transport_security(config: dict) -> str:
    """How the gateway will reach the utility, checked against NF-04
    ("connection established only with valid mutual certificates; rejected
    otherwise"). Raises ValueError for a configuration that would silently
    weaken what it asks for, and for anything weaker than mutual TLS to a
    host other than this machine unless allow_insecure_transport is set (a
    test network); otherwise returns a description for the log and the audit
    trail. No cipher or certificate profile is imposed here.

      https + CA + client cert/key   mutual TLS (NF-04), to any host
      https + CA only                server-only TLS (the simulator's DASHBOARD_TLS mode)
      https, no TLS settings         server-only TLS against the system trust store
      http                           plain HTTP (the simulator's default mode)
    The last three: this machine only, unless allow_insecure_transport."""
    url = config.get("ieee_server_url") or ""
    parts = urlsplit(url)
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    cert, ca = config.get("tls_client_cert"), config.get("tls_ca_cert")
    if scheme not in ("http", "https") or not host:
        raise ValueError(f"IEEE_SERVER_URL must be an http:// or https:// URL, got {url!r}")
    problem = _tls_settings_problem(config)
    if problem:
        raise ValueError(problem)
    if scheme == "https" and cert:
        return "mutual TLS 1.2+ (NF-04)"
    weaker = ("plain HTTP (no TLS)" if scheme == "http" else
              "server-only TLS 1.2+ (no client certificate)" if ca else
              "server-only TLS against the system trust store (no client certificate)")
    if host in _LOOPBACK_HOSTS:
        return f"{weaker} to the local simulator: not NF-04 mutual TLS"
    if config.get("allow_insecure_transport"):
        return f"{weaker} to {host} (ALLOW_INSECURE_TRANSPORT set): not NF-04 mutual TLS"
    raise ValueError(f"{weaker} to {host} is refused (NF-04 requires TLS with mutual certificates); "
                     f"use an https:// URL with TLS_CA_CERT, TLS_CLIENT_CERT and TLS_CLIENT_KEY, "
                     f"or set ALLOW_INSECURE_TRANSPORT=1 on a test network")


def _tls_settings_problem(config: dict) -> str | None:
    """TLS settings that would be silently dropped or ignored, else None."""
    cert, key, ca = config.get("tls_client_cert"), config.get("tls_client_key"), config.get("tls_ca_cert")
    if bool(cert) != bool(key):
        return "TLS_CLIENT_CERT and TLS_CLIENT_KEY must be set together"
    if cert and not ca:
        return ("a client certificate is configured without TLS_CA_CERT to verify the server, "
                "so it would be silently dropped")
    if (ca or cert) and urlsplit(config.get("ieee_server_url") or "").scheme.lower() == "http":
        return "TLS certificates are configured but IEEE_SERVER_URL is http://, so they would be silently ignored"
    return None


def build_mtls_context(config: dict) -> ssl.SSLContext | None:
    """Build a client-side TLS context for talking to the IEEE 2030.5
    server, in whichever of three modes the config describes. Returns
    None (plain HTTP, no TLS) if none of the TLS settings are present —
    this keeps the local simulator workflow (plain HTTP) working
    unchanged by default.

    Two TLS modes are supported, matching the two TLS modes
    fake_grid_server.py can run in:

      - Mutual TLS (NF-04: "IEEE 2030.5 communication SHALL use TLS 1.2
        or higher with mutual certificate authentication") — used when
        tls_client_cert + tls_client_key + tls_ca_cert are ALL set. The
        client both verifies the server's certificate against the given
        CA and presents its own certificate. This is the strict,
        machine-to-machine mode (TLS_ENABLED=1 on the server).

      - Server-only TLS — used when only tls_ca_cert is set (no client
        cert/key). The client verifies the server's certificate but
        presents none of its own — standard HTTPS, the same trust model
        a browser uses. This is what's needed to reach the server when
        it's running in DASHBOARD_TLS=1 mode, since that mode doesn't
        require or expect a client certificate.

    TLS settings that would be silently dropped or ignored raise
    ValueError, for every caller. (Plain HTTP to another host is refused
    at startup: see transport_security().)"""
    problem = _tls_settings_problem(config)
    if problem:
        raise ValueError(problem)
    cert = config.get("tls_client_cert")
    key  = config.get("tls_client_key")
    ca   = config.get("tls_ca_cert")
    if not ca:
        return None

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_ciphers(UTILITY_CIPHERS)      # the IEEE 2030.5 suite first (see UTILITY_CIPHERS)
    context.load_verify_locations(cafile=ca)  # trust the utility's CA (verify server)
    if cert and key:
        context.load_cert_chain(certfile=cert, keyfile=key)  # present our own cert (mutual auth)
    return context


def der_ratings(config: dict) -> dict:
    """The DER's IEEE 2030.5 DERSettings power ratings from the config, in
    W, by attribute name; None where not configured. Raises ValueError for
    a rating that is not a whole number of watts from 1 up to what the
    int32 kW setpoint register can hold, and for a storage rate configured
    without setMaxW."""
    ratings = {}
    for name, key in DER_RATING_SETTINGS:
        watts = config.get(key)
        if watts is not None and (isinstance(watts, bool) or not isinstance(watts, int)
                                  or not 0 < watts <= INT32_MAX * 1000):
            raise ValueError(f"{key.upper()} ({name}) must be a whole number of watts "
                             f"from 1 to {INT32_MAX * 1000}, got {watts!r}")
        ratings[name] = watts
    if ratings["setMaxW"] is None and (ratings["setMaxChargeRateW"] or ratings["setMaxDischargeRateW"]):
        raise ValueError("DER_SET_MAX_CHARGE_RATE_W and DER_SET_MAX_DISCHARGE_RATE_W need DER_SET_MAX_W")
    return ratings


def fixed_w_reference(ratings: dict, hundredths: int) -> tuple[str, int]:
    """The rating an opModFixedW value is a percentage of, as (DERSettings
    attribute, W). IEEE 2030.5-2018 DERControlBase: "in %setMaxChargeRateW
    if negative value or %setMaxW or %setMaxDischargeRateW if positive
    value (in hundredths)". A storage rate that is not configured falls
    back to setMaxW: the standard defaults it to the DER's rated charge or
    discharge power, which the gateway is not given. Raises ValueError
    without setMaxW."""
    name = ("setMaxDischargeRateW" if hundredths > 0 else
            "setMaxChargeRateW" if hundredths < 0 else "setMaxW")
    if ratings.get(name) is None:
        name = "setMaxW"
    if ratings.get(name) is None:
        raise ValueError("opModFixedW is a percentage of the DER's power ratings, "
                         "and DER_SET_MAX_W (setMaxW) is not configured")
    return name, ratings[name]


def describe_der_ratings(ratings: dict) -> str:
    """The ratings in one line, for the banner and the log."""
    if ratings["setMaxW"] is None:
        return "not configured (DER_SET_MAX_W): opModFixedW percentages are refused"
    (charge, charge_w), (discharge, discharge_w) = fixed_w_reference(ratings, -1), fixed_w_reference(ratings, 1)
    return (f"setMaxW {ratings['setMaxW'] / 1000:g} kW; opModFixedW charges against {charge} "
            f"{charge_w / 1000:g} kW, discharges against {discharge} {discharge_w / 1000:g} kW")


_EC_PUBLIC_KEY = bytes.fromhex("2a8648ce3d0201")     # OID 1.2.840.10045.2.1, id-ecPublicKey
_P256 = bytes.fromhex("2a8648ce3d030107")            # OID 1.2.840.10045.3.1.7, prime256v1 (P-256)
_RSA = bytes.fromhex("2a864886f70d010101")           # OID 1.2.840.113549.1.1.1, rsaEncryption


def _der_items(der: bytes, start: int, end: int):
    """(tag, value start, value end) of each DER item from start to end."""
    while start < end:
        tag, length = der[start], der[start + 1]
        start += 2
        if length & 0x80:
            count = length & 0x7F
            length = int.from_bytes(der[start:start + count], "big")
            start += count
        yield tag, start, start + length
        start += length


def certificate_key(path) -> str:
    """The public key of a certificate — "EC P-256", "EC (another curve)",
    "RSA <bits>" or "unknown" — for the start-up log: IEEE 2030.5 device
    certificates are ECDSA on P-256."""
    try:
        der = responses.certificate_der(path)
        _, start, end = next(_der_items(der, 0, len(der)))            # Certificate
        _, start, end = next(_der_items(der, start, end))             # tbsCertificate
        fields = list(_der_items(der, start, end))
        if fields[0][0] == 0xA0:                                     # [0] version
            fields = fields[1:]
        _, start, end = fields[5]                                    # subjectPublicKeyInfo
        (_, a_start, a_end), (_, k_start, k_end) = list(_der_items(der, start, end))[:2]
        algorithm = [der[s:e] for _, s, e in _der_items(der, a_start, a_end)]
        if algorithm[0] == _EC_PUBLIC_KEY:
            return "EC P-256" if algorithm[1:2] == [_P256] else "EC (another curve)"
        if algorithm[0] == _RSA:
            _, m_start, m_end = next(_der_items(der, k_start + 1, k_end))   # RSAPublicKey (after the unused-bits byte)
            _, n_start, n_end = next(_der_items(der, m_start, m_end))       # its modulus
            return f"RSA {int.from_bytes(der[n_start:n_end], 'big').bit_length()}"
    except (OSError, ValueError, IndexError, StopIteration):
        pass
    return "unknown"


def device_lfdi(config: dict) -> tuple[str | None, str]:
    """The gateway's LFDI, which identifies it in every DERControlResponse
    (endDeviceLFDI), and where it came from. IEEE 2030.5 derives it from the
    device certificate, so the TLS client certificate is used whenever one
    is configured; otherwise DEVICE_LFDI; otherwise, for the local
    simulator only, the development certificate. None when there is none:
    responses are then refused (and audited) rather than sent without it.
    Raises ValueError for an unreadable certificate or a DEVICE_LFDI that
    is not 40 hex digits or disagrees with the certificate."""
    explicit = config.get("device_lfdi")
    if explicit is not None and not _LFDI.fullmatch(explicit):
        raise ValueError(f"DEVICE_LFDI must be 40 hex digits, got {explicit!r}")
    cert = config.get("tls_client_cert")
    if cert:
        try:
            lfdi = responses.lfdi_of_certificate(cert)
        except (OSError, ValueError) as e:
            raise ValueError(f"cannot read the LFDI from TLS_CLIENT_CERT: {e}") from None
        if explicit and explicit.upper() != lfdi:
            raise ValueError(f"DEVICE_LFDI {explicit} is not the LFDI of TLS_CLIENT_CERT ({lfdi})")
        return lfdi, "TLS client certificate"
    if explicit:
        return explicit.upper(), "DEVICE_LFDI"
    host = (urlsplit(config.get("ieee_server_url") or "").hostname or "").lower()
    if host in _LOOPBACK_HOSTS and DEV_CERTIFICATE.exists():
        return responses.lfdi_of_certificate(DEV_CERTIFICATE), "development certificate (local simulator only)"
    return None, "none: set DEVICE_LFDI or a TLS client certificate"


class IEEEClient:
    def __init__(self, config: dict, state, scheduler, reliability_manager=None, audit_log=None):
        self.url       = config["ieee_server_url"]
        self.dcap_path = config.get("ieee_dcap_path") or "/dcap"   # DeviceCapability, the entry point
        self.interval  = config["ieee_poll_interval_sec"]          # when the utility gives no pollRate
        self.poll_rate = None                                      # the utility's pollRate, once read
        self._discovered = None                                    # what the last poll found, for the audit
        self.timeout   = config["offline_timeout_sec"]
        self.state     = state
        self.scheduler = scheduler
        self.reliability_manager = reliability_manager  # optional — F-05 FSM
        self.audit_log = audit_log                       # optional — NF-03
        self._ssl_context = build_mtls_context(config)    # optional — NF-04 TLS (mutual or server-only)
        self._client_cert_configured = bool(config.get("tls_client_cert") and config.get("tls_client_key"))
        self._tls_seen = None                              # (TLS version, cipher suite) last audited
        self._tls_check_due = True                         # check them after the next successful poll
        self._client_cert_path = config.get("tls_client_cert")
        self.transport = config.get("transport")           # checked description, from load_config()
        self.der_ratings = der_ratings(config)             # what opModFixedW percentages refer to
        self.lfdi, self.lfdi_source = device_lfdi(config)  # endDeviceLFDI of every response
        self._responses_waiting = asyncio.Event()          # set by a poll that may have queued responses
        self.links = ReportLinks()                         # where reports go (capstone/reporting.py)
        self._time_due = 0.0                               # monotonic time the Time resource is next read
        self._clock_audited = None                         # clock offset last audited
        self._der_found = None                             # DER link last audited

    async def run(self):
        log.info(f"IEEE client: DeviceCapability {self._resource_url(self.dcap_path)}; polling at the "
                 f"utility's pollRate, else every {self.interval}s")
        if self.transport:
            log.log(logging.INFO if self.transport.startswith("mutual") else logging.WARNING,
                    f"Utility transport: {self.transport}")
        log.log(logging.INFO if self.der_ratings["setMaxW"] else logging.WARNING,
                f"DER ratings: {describe_der_ratings(self.der_ratings)}")
        log.log(logging.INFO if self.lfdi else logging.WARNING,
                f"LFDI for DERControlResponses: {self.lfdi or '-'} ({self.lfdi_source})")
        if self._client_cert_configured:
            kind = certificate_key(self._client_cert_path)
            log.log(logging.INFO if kind == "EC P-256" else logging.WARNING,
                    f"Client certificate key: {kind}" + ("" if kind == "EC P-256" else
                    " — IEEE 2030.5 device certificates are ECDSA on P-256; a utility may refuse it"))
        async with self._session() as session:
            while True:
                started = time.monotonic()
                ok = await self._poll(session)
                # Every pollRate seconds from the start of one poll to the next,
                # not pollRate seconds of idling after it.
                await asyncio.sleep(max(0.0, self.next_poll_in(ok) - (time.monotonic() - started)))

    def next_poll_in(self, last_poll_ok: bool) -> int:
        """Seconds to the next poll: the utility's pollRate when it gives one
        (IEEE 2030.5: "RECOMMENDED a client poll ... every pollRate
        seconds"), else IEEE_POLL_INTERVAL_SEC. After a failed poll, no
        longer than IEEE_POLL_INTERVAL_SEC, so a lost link is noticed, and a
        restored one used, without waiting a whole pollRate."""
        rate = self.poll_rate or self.interval
        return rate if last_poll_ok else min(rate, self.interval)

    def _session(self) -> aiohttp.ClientSession:
        connector = aiohttp.TCPConnector(ssl=self._ssl_context) if self._ssl_context else None
        return aiohttp.ClientSession(connector=connector)

    async def response_loop(self):
        """Deliver queued DERControlResponses, off the polling path: a slow
        or unreachable utility never delays the next poll. Wakes when a poll
        may have queued some, and at least once a second for the ones the
        scheduler queues (started, completed, ...) and for retries."""
        async with self._session() as session:
            while True:
                self._responses_waiting.clear()
                await self.send_responses(session)
                try:
                    await asyncio.wait_for(self._responses_waiting.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

    async def send_responses(self, session: aiohttp.ClientSession, now: float | None = None) -> int:
        """Send every response that is due at `now` (default: the current
        time), a few at a time. An event's responses go out in order: a
        later one follows in the same call once the earlier one is
        delivered. Returns how many POSTs were attempted."""
        now = time.time() if now is None else now
        gate = asyncio.Semaphore(RESPONSE_CONCURRENCY)

        async def one(row):
            async with gate:
                await self._deliver(session, now, *row)
        attempted = 0
        # Each round moves every response it sends out of "due now" (delivered,
        # given up, or retried no sooner than 5 s later), so this ends.
        while due := list(await asyncio.to_thread(self.scheduler.due_responses, now, RESPONSE_BATCH)):
            await asyncio.gather(*(one(row) for row in due))
            attempted += len(due)
        return attempted

    async def _deliver(self, session, now: float, mrid: str, status: int, reply_to: str,
                       created_at: float, failed_before: int):
        label = f"DERControlResponse {responses.STATUS_NAMES.get(status, status)} for {mrid}"
        if self.lfdi is None:
            return await self._give_up(now, mrid, status, f"{label}: not sent: no LFDI ({self.lfdi_source})")
        url = self._resource_url(reply_to)
        if url is None:
            return await self._give_up(now, mrid, status, f"{label}: not sent: replyTo {reply_to!r} is not "
                                                          f"on the utility server {self.url}")
        where = f"(status {status} to {urlsplit(url).path})"
        try:
            # createdDateTime on the utility's clock (see _sync_time).
            created = created_at + self.state.clock_offset_sec
            async with session.post(url, data=_response_body(mrid, status, self.lfdi, created),
                                    headers={"Content-Type": "application/sep+xml"},
                                    timeout=aiohttp.ClientTimeout(total=5)) as resp:
                await resp.read()                          # frees the connection for reuse
                code = resp.status
        except Exception as e:
            return await self._retry(now, mrid, status, created_at,
                                     f"{label}: {type(e).__name__}: {e} {where}", "failed")
        if code in (200, 201, 204):
            await asyncio.to_thread(self.scheduler.response_delivered, mrid, status, now)
            log.info(f"{label} sent: HTTP {code}")
            self._audit("ieee_response", f"{label}: HTTP {code} {where}"
                        + (f" after {failed_before} failed attempt(s)" if failed_before else ""), "sent")
        elif 400 <= code < 500 and code not in (408, 429):
            # The server refuses this response itself: sending it again cannot help.
            await self._give_up(now, mrid, status, f"{label}: HTTP {code} {where}; not retried", "rejected")
        else:
            await self._retry(now, mrid, status, created_at, f"{label}: HTTP {code} {where}", "rejected")

    async def _retry(self, now, mrid, status, created_at, detail: str, outcome: str):
        if now - created_at > responses.GIVE_UP_AFTER_SEC:
            return await self._give_up(now, mrid, status, f"{detail}; undelivered for "
                                                          f"{responses.GIVE_UP_AFTER_SEC // 3600} h, given up")
        failed = await asyncio.to_thread(self.scheduler.response_failed, mrid, status, detail, now)
        log.warning(f"{detail} (attempt {failed}; retrying with backoff)")
        if failed == 1:                                    # audited when it starts failing, not every retry
            self._audit("ieee_response", f"{detail}; retrying with backoff", outcome)

    async def _give_up(self, now, mrid, status, detail: str, outcome: str = "abandoned"):
        await asyncio.to_thread(self.scheduler.response_given_up, mrid, status, detail, now)
        log.error(detail)
        self._audit("ieee_response", detail, outcome)

    def _resource_url(self, href: str) -> str | None:
        """An href (or replyTo) resolved against the utility server, or None
        if it points anywhere else: the gateway only ever talks to the server
        it was configured with, over its client certificate."""
        url = urljoin(self.url.rstrip("/") + "/", href)
        if _origin(url) is None or _origin(url) != _origin(self.url):
            return None
        return url

    async def _poll(self, session: aiohttp.ClientSession) -> bool:
        """One poll: read every DER program assigned to this device and hand
        its events and DefaultDERControl to the scheduler. Returns whether
        the utility could be read."""
        try:
            programs, poll_rate = await self._discover(session)
        except _LinkFailure as e:
            log.error(f"Cannot read the IEEE 2030.5 server — grid connection lost ({e})")
            self._link_failure(str(e))
            return False
        except Exception as e:
            # Last-resort catch: an unexpected exception here must never
            # silently kill the polling loop — that would stop ALL future
            # polling forever with no visible error.
            log.exception("Unexpected error while polling IEEE server")
            self._link_failure(str(e))
            return False

        self.state.mark_grid_contact()
        self._record_poll_outcome(success=True)
        if self._ssl_context is not None and self._tls_check_due:
            await self._check_tls()
        self._note_discovery(programs, poll_rate)
        try:
            # Storing events is SQLite work: off the event loop (NF-01), as in the scheduler.
            # The responses they call for are queued there and sent by response_loop().
            await asyncio.to_thread(self._dispatch_programs, programs)
        except Exception as e:
            # A local failure after a good response is not a utility-link loss.
            log.exception("Processing the utility's DER programs failed")
            self._audit("poll_processing", f"{type(e).__name__}: {e}", "failed")
        self._responses_waiting.set()
        return True

    async def _discover(self, session) -> tuple[list[Program], int | None]:
        """Every DER program this device is to follow, read in full (CSIP
        §5.2.3.1): those DeviceCapability lists for all devices
        (DERProgramListLink) and those of each FunctionSetAssignments of this
        device's EndDevice, matched by LFDI or SFDI. The 15 with the lowest
        primacy (CSIP §5.2.3), lowest first, each with its DERControls and
        DefaultDERControl; and the smallest pollRate their lists give."""
        dcap = await self._get(session, self.dcap_path, "DeviceCapability")
        links = ReportLinks(time=_href(dcap, "TimeLink"), mirror_usage_points=_href(dcap, "MirrorUsagePointListLink"))
        await self._sync_time(session, links.time)
        program_lists = [_href(dcap, "DERProgramListLink")]
        der_lists = []
        if _href(dcap, "EndDeviceListLink"):
            devices, _ = await self._get_list(session, _href(dcap, "EndDeviceListLink"), "EndDeviceList", "EndDevice")
            for device in filter(self._is_this_device, devices):
                if _href(device, "FunctionSetAssignmentsListLink"):
                    assignments, _ = await self._get_list(session, _href(device, "FunctionSetAssignmentsListLink"),
                                                          "FunctionSetAssignmentsList", "FunctionSetAssignments")
                    program_lists += [_href(fsa, "DERProgramListLink") for fsa in assignments]
                der_lists += [_href(device, "DERListLink")]
        await self._find_der(session, next(filter(None, der_lists), None), links)
        self.links = links

        found, rates = {}, []
        for href in dict.fromkeys(filter(None, program_lists)):
            items, first_page = await self._get_list(session, href, "DERProgramList", "DERProgram")
            rates += [rate for rate in [_int_attribute(first_page, "pollRate")] if rate is not None]
            for item in items:
                found.setdefault(item.get("href") or f"{href}#{items.index(item)}", item)
        ranked = sorted(found.items(), key=lambda item: _primacy(item[1]))
        if len(ranked) > MAX_PROGRAMS:
            log.warning(f"{len(ranked)} DER programs listed: following the {MAX_PROGRAMS} with the lowest primacy")

        programs = []
        for href, item in ranked[:MAX_PROGRAMS]:
            controls = []
            if _href(item, "DERControlListLink"):
                controls, _ = await self._get_list(session, _href(item, "DERControlListLink"),
                                                   "DERControlList", "DERControl")
            default = None
            if _href(item, "DefaultDERControlLink"):
                try:
                    default = await self._get(session, _href(item, "DefaultDERControlLink"), "DefaultDERControl")
                except _LinkFailure as e:
                    log.warning(f"DefaultDERControl of {href} not read ({e}): keeping the one last read")
                    default = KEEP
            programs.append(Program(href, _primacy(item), controls, default))
        return programs, min(rates) if rates else None

    async def _sync_time(self, session, href: str | None):
        """Read the utility's Time resource when due (IEEE 2030.5 Time
        function set: the client keeps to the server's time) and keep the
        offset of its clock from this machine's, which the scheduler adds
        when it compares event windows with now. Estimated at the middle of
        the request; offsets within CLOCK_DEADBAND_SEC count as 0. A Time
        that cannot be read keeps the offset last found."""
        if href is None or time.monotonic() < self._time_due:
            return
        sent = time.time()
        try:
            root = await self._get(session, href, "Time")
        except _LinkFailure as e:
            log.warning(f"Utility Time {href} not read ({e}): keeping the clock offset "
                        f"{self.state.clock_offset_sec:+.1f} s")
            return
        received = time.time()
        current = _int_text(root, "currentTime")
        if current is None:
            log.warning(f"Utility Time {href} has no currentTime: keeping the clock offset "
                        f"{self.state.clock_offset_sec:+.1f} s")
            return
        low, high = POLL_RATE_BOUNDS
        self._time_due = time.monotonic() + max(low, min(high, _int_attribute(root, "pollRate") or TIME_POLL_RATE))
        offset = current + 0.5 - (sent + received) / 2        # currentTime is the whole second it was read in
        offset = 0.0 if abs(offset) < CLOCK_DEADBAND_SEC else round(offset, 1)
        self.state.clock_offset_sec = offset
        if self._clock_audited is None or abs(offset - self._clock_audited) >= CLOCK_DEADBAND_SEC:
            self._clock_audited = offset
            detail = (f"utility Time {href}: this machine's clock agrees with the utility's within "
                      f"{CLOCK_DEADBAND_SEC:g} s" if offset == 0 else
                      f"utility Time {href}: the utility's clock is {offset:+.1f} s from this machine's; "
                      f"event times are taken on the utility's")
            log.log(logging.INFO if offset == 0 else logging.WARNING, detail)
            self._audit("ieee_time", detail, "synchronized")

    async def _find_der(self, session, href: str | None, links: ReportLinks):
        """This device's DER, from its EndDevice's DERList: where its
        capability, settings and status are reported (the gateway is one
        DER; the first listed is used). A DERList that cannot be read keeps
        the links found before: reporting is not dispatch, and the poll
        goes on."""
        if href is None:
            return self._note_der("the utility lists no DER for this device")
        try:
            ders, first_page = await self._get_list(session, href, "DERList", "DER")
        except _LinkFailure as e:
            previous = self.links
            for name in ("der", "capability", "settings", "status", "availability", "status_rate"):
                setattr(links, name, getattr(previous, name))
            log.warning(f"DERList {href} not read ({e}): reporting to the DER found before")
            return
        if not ders:
            return self._note_der(f"DERList {href} is empty")
        der = ders[0]
        links.der = der.get("href") or href
        links.capability, links.settings = _href(der, "DERCapabilityLink"), _href(der, "DERSettingsLink")
        links.status, links.availability = _href(der, "DERStatusLink"), _href(der, "DERAvailabilityLink")
        links.status_rate = _int_attribute(first_page, "pollRate")
        self._note_der(f"DER {links.der}" + (f" (the first of {len(ders)} listed)" if len(ders) > 1 else ""))

    def _note_der(self, detail: str):
        if detail != self._der_found:
            self._der_found = detail
            log.info(f"IEEE 2030.5 reporting: {detail}")
            self._audit("ieee_reporting", f"reports go to: {detail}", "found")

    async def _get(self, session, href: str, expected: str):
        """GET one IEEE 2030.5 resource; its parsed root element. Raises
        _LinkFailure if it cannot be read or is not the expected resource (a
        proxy error page, a captive portal: the utility was not reached)."""
        url = self._resource_url(href)
        if url is None:
            raise _LinkFailure(f"{href} is not on the utility server")
        try:
            async with session.get(url, headers={"Accept": "application/sep+xml"},
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.warning(f"IEEE server returned HTTP {resp.status} for {href}")
                    raise _LinkFailure(f"HTTP {resp.status}")
                text = await resp.text()
        except asyncio.TimeoutError:
            raise _LinkFailure("timeout") from None
        except aiohttp.ClientError as e:
            # A new connection failing raises ClientConnectorError; a server
            # dying mid-session raises ServerDisconnectedError / ClientOSError.
            raise _LinkFailure(type(e).__name__) from None
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            root = None
        if root is None or root.tag.split("}")[-1] != expected:
            raise _LinkFailure(f"response is not an IEEE 2030.5 {expected}")
        return root

    async def _get_list(self, session, href: str, expected: str, item: str) -> tuple[list, object]:
        """Every item of a list resource, requested a page at a time (the
        `s` and `l` query parameters) until as many as its `all` attribute
        says have been read. Returns (items, first page)."""
        if self._resource_url(href) is None:
            raise _LinkFailure(f"{href} is not on the utility server")
        items, first_page = [], None
        for _ in range(MAX_PAGES):
            page = await self._get(session, f"{href}{'&' if '?' in href else '?'}s={len(items)}&l={PAGE_LIMIT}",
                                   expected)
            first_page = page if first_page is None else first_page
            found = page.findall(f"{{*}}{item}")
            items += found
            total = _int_attribute(page, "all")
            if total is None or len(items) >= total:
                return items, first_page
            if not found:
                log.warning(f"{href} ended after {len(items)} of the {total} items it lists")
                return items, first_page
        raise _LinkFailure(f"{href} did not end after {MAX_PAGES} pages")

    async def _check_tls(self):
        """Audit the TLS version and cipher suite the utility server chooses
        for this gateway's TLS settings, when they change: whether it is the
        suite IEEE 2030.5 makes mandatory is known only once the server has
        chosen. One extra handshake after the first successful poll and after
        each link failure (aiohttp does not tell a pooled connection's)."""
        self._tls_check_due = False
        parts = urlsplit(self.url)
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(
                parts.hostname, parts.port or 443, ssl=self._ssl_context, server_hostname=parts.hostname), timeout=10)
        except Exception as e:
            log.warning(f"TLS check of the utility connection failed: {type(e).__name__}: {e}")
            self._tls_check_due = True
            return
        try:
            tls = writer.get_extra_info("ssl_object")
            seen = (tls.version(), tls.cipher()[0])
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if seen == self._tls_seen:
            return
        self._tls_seen = seen
        standard = seen[1] == IEEE_2030_5_SUITE
        detail = (f"utility connection uses {seen[0]} {seen[1]}"
                  + (" (the IEEE 2030.5 cipher suite)" if standard else
                     f" (the server did not choose IEEE 2030.5's {IEEE_2030_5_SUITE})"))
        log.log(logging.INFO if standard else logging.WARNING, detail)
        self._audit("ieee_transport", detail, "negotiated")

    def _is_this_device(self, device) -> bool:
        """Is this EndDevice the gateway? Matched by LFDI, or by the SFDI
        derived from it."""
        if not self.lfdi:
            return False
        lfdi = (device.findtext("{*}lFDI") or "").strip().upper()
        sfdi = (device.findtext("{*}sFDI") or "").strip()
        return lfdi == self.lfdi or sfdi == str(responses.sfdi_of_lfdi(self.lfdi))

    def _note_discovery(self, programs: list[Program], poll_rate: int | None):
        """Use the utility's pollRate, and audit what the poll found when it
        changes: the programs followed and the poll rate."""
        low, high = POLL_RATE_BOUNDS
        self.poll_rate = None if poll_rate is None else max(low, min(high, poll_rate))
        found = (tuple((p.href, p.primacy) for p in programs), self.poll_rate)
        if found == self._discovered:
            return
        self._discovered = found
        detail = (f"{len(programs)} DER program(s): "
                  + (", ".join(f"{p.href} (primacy {p.primacy if p.primacy != NO_PRIMACY else 'none'})"
                               for p in programs) or "none")
                  + (f"; utility pollRate {self.poll_rate} s" if self.poll_rate
                     else f"; no pollRate: polling every {self.interval} s"))
        log.info(f"IEEE 2030.5 discovery: {detail}")
        self._audit("ieee_discovery", detail, "found")

    def _link_failure(self, detail: str):
        self._tls_check_due = True             # whatever answers next is checked again
        self.state.mark_grid_lost()
        self._record_poll_outcome(success=False, detail=detail)

    def _record_poll_outcome(self, success: bool, detail: str = ""):
        """Feed the poll result into the F-05 reliability FSM and the
        NF-03 audit log — both optional, so this is a no-op if either
        wasn't wired in (e.g. in unit tests). The FSM reads the dispatch
        arbiter's decision itself to choose AUTONOMOUS vs SAFE_IDLE."""
        if self.reliability_manager:
            if success:
                self.reliability_manager.record_poll_success()
            else:
                self.reliability_manager.record_poll_failure()
        if not success:
            self._audit("grid_poll", detail, "failed")

    def _audit(self, event_type: str, detail: str, outcome: str):
        if self.audit_log:
            self.audit_log.record(event_type=event_type, source="ieee_client",
                                  detail=detail, outcome=outcome)

    def _parse_and_dispatch(self, xml_text: str) -> list:
        """Parse and hand off every DERControl event found. Returns the
        list of events handed to the scheduler. Synchronous, so it stays
        independently unit-testable; the DERControlResponses this calls
        for are queued, and sent by response_loop()."""
        root = _parse_der_control_list(xml_text)
        if root is None:
            log.error("Not an IEEE 2030.5 DERControlList — nothing dispatched")
            return []
        return self._dispatch_controls(root)

    def _dispatch_controls(self, root, program: Program | None = None) -> list:
        """Hand every DERControl of a DERControlList to the scheduler;
        returns the events it accepted."""
        controls = root.findall("{*}DERControl")
        if not controls:
            log.debug("No DERControl events in response.")
        return [event for event in (self._dispatch_control(ctrl, program) for ctrl in controls) if event]

    def _dispatch_programs(self, programs: list[Program]):
        """Hand every program's events, then the DefaultDERControls, to the scheduler."""
        for program in programs:
            for ctrl in program.controls:
                self._dispatch_control(ctrl, program)
        self.scheduler.set_default_controls(
            [self._default_control(p) for p in programs if p.default is not None and p.default is not KEEP],
            keep=[p.href for p in programs if p.default is KEEP])

    def _dispatch_control(self, ctrl, program: Program | None) -> dict | None:
        # Each event is handled on its own: one malformed or unstorable
        # event must not drop the others.
        try:
            event = self._extract_event(ctrl, program)
            if event:
                log.debug(f"DERControl event read: {event}")
                self.scheduler.add_event(event)
            return event
        except Exception as e:
            mrid = ctrl.findtext("{*}mRID", default="unknown")
            log.error(f"DERControl {mrid} not accepted: {type(e).__name__}: {e}")
            self._audit("schedule_rejected", f"DERControl {mrid}: {type(e).__name__}: {e}", "error")
            return None

    def _extract_event(self, ctrl_elem, program: Program | None = None) -> dict | None:
        mrid = ctrl_elem.findtext("{*}mRID")

        # The mRID is the event's identity: it is how the server acknowledges,
        # cancels or supersedes it. Two events without one were both stored
        # as "unknown", the second overwriting the first.
        if mrid is None or not mrid.strip():
            start = ctrl_elem.findtext("{*}interval/{*}start")
            detail = f"DERControl rejected: no mRID (interval start {start})"
            log.warning(detail)
            self._audit("schedule_rejected", detail, "invalid")
            return None

        # Where and whether the server wants DERControlResponses for it.
        reply = ((ctrl_elem.get("replyTo") or "").strip() or None,
                 responses.parse_response_required(ctrl_elem.get("responseRequired")))

        try:
            output_kw, control = self._control_setpoint(ctrl_elem.find("{*}DERControlBase"))
            start_ts = int(ctrl_elem.findtext("{*}interval/{*}start", default="0"))
            duration = int(ctrl_elem.findtext("{*}interval/{*}duration", default="0"))

            # Numeric safety: integers only (int() refuses "nan", "inf",
            # "1.5"), scaled exactly, and within what the int32 kW setpoint
            # register can express; nothing is wrapped or rounded into a
            # different command.
            if duration < 0:
                raise ValueError(f"negative duration {duration} s")
            end_ts = start_ts + duration
            for t in (start_ts, end_ts):
                if not _is_utc_date(t):
                    raise ValueError(f"time {t} is not a UTC date in years 1-9999")
            # IEEE 2030.5 counts the power the DER puts out (+ = discharge);
            # the gateway's convention is + = charge.
            exact_kw = -output_kw
            if abs(exact_kw) > INT32_MAX:
                raise ValueError(f"{control} is outside the int32 kW setpoint range")
            power_kw = float(exact_kw)
            for name in ("randomizeStart", "randomizeDuration"):
                value = (ctrl_elem.findtext(f"{{*}}{name}") or "0").strip()
                if value != "0":
                    control += f"; {name} {value} s not applied (no randomization)"

            # creationTime ranks overlapping events of equal primacy: the
            # newer one supersedes (IEEE 2030.5-2018 §10.2.3.3).
            raw_creation = ctrl_elem.findtext("{*}creationTime")
            creation_time = None if raw_creation is None else int(raw_creation)

            # EventStatus.currentStatus, when the server sends it: the
            # scheduler never executes an event the server marks Cancelled
            # (2, 3) or Superseded (4). Anything that isn't an integer is
            # kept as-is so the scheduler rejects it rather than guessing.
            raw_status = ctrl_elem.findtext("{*}EventStatus/{*}currentStatus")
            try:
                server_status = None if raw_status is None else int(raw_status)
            except ValueError:
                server_status = raw_status

            return {
                "mrid":     mrid,
                "start":    start_ts,
                "end":      end_ts,
                "power_kw": power_kw,
                "source":   "ieee2030.5",
                "server_status": server_status,
                "control":  control,          # what the utility sent, for the audit trail
                "reply_to": reply[0],
                "response_required": reply[1],
                "creation_time": creation_time,
                "primacy":  None if program is None else program.primacy,
                "program":  None if program is None else program.href,
            }
        except _Refused as refused:
            return self._reject(f"DERControl {mrid} rejected: {refused}", refused.outcome, mrid, reply)
        except (ValueError, TypeError, OverflowError) as e:
            log.error(f"Failed to parse DERControl element: {e}")
            self._audit("schedule_rejected", f"DERControl {mrid}: unparseable ({e})", "invalid")
            self._queue_refusal(mrid, responses.REJECTED_INVALID, reply)
            return None

    def _control_setpoint(self, base) -> tuple[Fraction, str]:
        """What a DERControlBase asks for: the DER's output in kW, exactly
        (IEEE 2030.5 sign: + = discharge), and a description of the control.
        Raises _Refused for a control the gateway will not execute, a
        malformed structure or no setpoint at all, and ValueError /
        TypeError / OverflowError for a malformed number."""
        found = [c.tag.split("}")[-1] for c in base] if base is not None else []
        controls = {name: base.find(f"{{*}}{name}") for name in found}
        if len(controls) != len(found):
            raise _Refused("invalid", f"a control appears twice in DERControlBase ({', '.join(found)})")
        # A control the gateway cannot execute makes it refuse the whole event.
        unsupported = [name for name in found if name not in EXECUTABLE_CONTROLS]
        if unsupported:
            raise _Refused("unsupported", f"{', '.join(unsupported)} cannot be executed by this gateway")

        # opModConnect / opModEnergize false: disconnect or cease to energize,
        # so 0 kW whatever setpoint the event also carries.
        switched_off = []
        for name in ("opModConnect", "opModEnergize"):
            if name in controls:
                text = (controls[name].text or "").strip().lower()
                if text not in ("true", "false", "1", "0"):
                    raise _Refused("invalid", f"{name} must be true or false, got {text!r}")
                if text in ("false", "0"):
                    switched_off.append(f"{name} false")

        # Otherwise the active-power setpoint, in either form CSIP lists for
        # "Set Active Power Mode" (Table 9): opModFixedW, a signed percentage
        # of the DER's power ratings, or opModTargetW, in watts. An event
        # without one has nothing for us to apply — silently defaulting it
        # to 0 kW would look like a valid "go idle" order, so reject it
        # loudly instead.
        fixed, target = controls.get("opModFixedW"), controls.get("opModTargetW")
        if switched_off:
            output_kw, control = Fraction(0), f"{' and '.join(switched_off)}: 0 kW"
        elif fixed is None and target is None:
            raise _Refused("unsupported", f"no opModFixedW or opModTargetW setpoint "
                                          f"(found: {', '.join(found) or 'no DERControlBase controls'})",
                           "no_setpoint")
        elif fixed is not None and target is not None:
            raise _Refused("invalid", "it carries both opModFixedW and opModTargetW, "
                                      "and which one applies is not defined")
        elif fixed is not None:
            if self.der_ratings["setMaxW"] is None:
                raise _Refused("unsupported", "opModFixedW is a percentage of the DER's power ratings, "
                                              "and DER_SET_MAX_W (setMaxW) is not configured")
            output_kw, control = _fixed_w_output_kw(fixed, self.der_ratings)
        else:
            output_kw, control = _target_w_output_kw(target)
        if "rampTms" in controls:
            control += "; rampTms not applied (the BMS ramps at its own rate)"
        return output_kw, control

    def _default_control(self, program: Program) -> dict:
        """A program's DefaultDERControl as the scheduler keeps it: the power
        it asks for between events (the gateway's convention, + = charge;
        None when it asks for no power), and why it cannot be honoured when
        the gateway cannot execute it as a whole."""
        element, power_kw, problem = program.default, None, None
        try:
            output_kw, control = self._control_setpoint(element.find("{*}DERControlBase"))
            if abs(output_kw) > INT32_MAX:
                raise ValueError(f"{control} is outside the int32 kW setpoint range")
            power_kw = float(-output_kw)
        except _Refused as refused:
            if refused.kind == "no_setpoint":
                control = "no active-power setpoint"
            else:
                control = problem = str(refused)
        except (ValueError, TypeError, OverflowError) as e:
            control = problem = f"unparseable ({e})"
        # Its own settings (ramp rates, enter-service limits) are for the
        # inverter itself; the gateway has no way to apply them.
        settings = [child.tag.split("}")[-1] for child in element
                    if child.tag.split("}")[-1] not in ("DERControlBase", "mRID", "description", "version")]
        if settings:
            control += f"; {', '.join(settings)} not applied"
        return {"program": program.href, "primacy": program.primacy, "power_kw": power_kw,
                "control": control, "problem": problem}

    def _reject(self, detail: str, outcome: str, mrid: str, reply) -> None:
        """Refuse an event: log, audit, and queue the refusal the server
        asked for: 252 (not applicable) for what the gateway cannot
        execute, 253 (invalid event) for what is malformed."""
        log.warning(detail)
        self._audit("schedule_rejected", detail, outcome)
        self._queue_refusal(mrid, responses.REJECTED_NOT_APPLICABLE if outcome == "unsupported"
                            else responses.REJECTED_INVALID, reply)
        return None

    def _queue_refusal(self, mrid: str, status: int, reply):
        try:
            self.scheduler.record_response(mrid, status, *reply)
        except Exception:
            log.exception(f"Could not queue the {responses.STATUS_NAMES[status]} response for {mrid}")


def _fixed_w_output_kw(elem, ratings: dict) -> tuple[Fraction, str]:
    """opModFixedW, the IEEE 2030.5 SignedPerCent (hundredths of a percent,
    -10000..10000), as the DER's output in kW, exactly, and a description
    of the conversion."""
    if len(elem):
        raise ValueError("opModFixedW must be a number of hundredths of a percent (IEEE 2030.5 "
                         "SignedPerCent), not <value>/<multiplier> elements")
    text = (elem.text or "").strip()
    if not text:
        raise ValueError("opModFixedW is empty")
    hundredths = int(text)
    if not -PERCENT_SCALE <= hundredths <= PERCENT_SCALE:
        raise ValueError(f"opModFixedW {hundredths} is outside -10000..10000 (hundredths of a percent)")
    name, watts = fixed_w_reference(ratings, hundredths)
    output_kw = Fraction(hundredths * watts, PERCENT_SCALE * 1000)
    return output_kw, f"opModFixedW {hundredths / 100:+.2f}% of {name} {watts} W"


def _target_w_output_kw(elem) -> tuple[Fraction, str]:
    """opModTargetW, an IEEE 2030.5 ActivePower (value x 10^multiplier W),
    as the DER's output in kW, exactly, and a description of it. A missing
    value is refused rather than read as 0 kW."""
    raw_value = elem.findtext("{*}value")
    if raw_value is None:
        raise ValueError("opModTargetW has no value")
    value = int(raw_value)
    mult = int(elem.findtext("{*}multiplier", default="0"))
    if abs(mult) > MAX_MULTIPLIER:
        raise ValueError(f"power multiplier 10^{mult} is outside ±{MAX_MULTIPLIER}")
    output_kw = Fraction(value) * Fraction(10) ** mult / 1000
    return output_kw, f"opModTargetW {value} x 10^{mult} W"


def _href(element, link: str) -> str | None:
    """The href of one of an element's links (e.g. DERControlListLink), or None."""
    found = element.find(f"{{*}}{link}")
    if found is None:
        return None
    return (found.get("href") or "").strip() or None


def _int_text(element, name: str) -> int | None:
    """The integer value of a child element, or None if missing or not a whole number."""
    try:
        return int((element.findtext(f"{{*}}{name}") or "").strip())
    except ValueError:
        return None


def _int_attribute(element, name: str) -> int | None:
    try:
        return int(element.get(name))
    except (TypeError, ValueError):
        return None


def _primacy(program) -> int:
    """A DERProgram's primacy (0-255; the lower value wins), NO_PRIMACY if it has none."""
    try:
        value = int((program.findtext("{*}primacy") or "").strip())
    except ValueError:
        return NO_PRIMACY
    return value if 0 <= value <= 255 else NO_PRIMACY


def _origin(url: str) -> tuple | None:
    """(scheme, host, port) of an http(s) URL, the port made explicit."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:                                  # a port that is not a number
        return None
    return scheme, parts.hostname.lower(), port


def _response_body(mrid: str, status: int, lfdi: str, created_at: float) -> str:
    """A DERControlResponse (IEEE 2030.5 Response: createdDateTime,
    endDeviceLFDI, status, subject, in schema order). The subject is the
    event's mRID, the utility's own data, escaped."""
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<DERControlResponse xmlns="urn:ieee:std:2030.5:ns">'
            f"<createdDateTime>{int(created_at)}</createdDateTime>"
            f"<endDeviceLFDI>{lfdi}</endDeviceLFDI>"
            f"<status>{status}</status>"
            f"<subject>{xml_escape(mrid)}</subject>"
            "</DERControlResponse>")


def _parse_der_control_list(xml_text: str):
    """The parsed root element if xml_text is an IEEE 2030.5
    DERControlList, else None."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.error(f"XML parse error: {e}")
        return None
    if root.tag.split("}")[-1] != "DERControlList":
        return None
    return root