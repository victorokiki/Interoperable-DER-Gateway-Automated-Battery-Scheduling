"""
capstone/reliability.py
Reliability State Manager — 5-state FSM (Requirement F-05)

States (per Sprint 1 Report, Table 2, renamed per Sprint 2 partner
feedback — DERATED more accurately describes a system operating with
reduced capabilities than DEGRADED, which incorrectly implied failure):

    CONNECTED     Normal operation. Grid reachable.
    DERATED       Grid contact is intermittent (some missed polls),
                  but not yet past the disconnect threshold.
    DISCONNECTED  Grid unreachable, and the battery is not confirmed to
                  be doing either of the two things below yet (within
                  the offline grace period with no approved schedule, or
                  the BMS has not confirmed the setpoint).
    AUTONOMOUS    Grid unreachable, but a previously-approved stored
                  schedule is still valid and the BMS has confirmed it
                  is being executed (Sprint 2 Table 1: continue until
                  completion or expiry), or, between events, the
                  utility's DefaultDERControl is.
    SAFE_IDLE     Grid unreachable beyond the grace period AND no
                  valid stored schedule remains — the BMS has confirmed
                  the battery is at the safe 0 kW setpoint.

The disconnected sub-states follow what the scheduler's dispatch arbiter
actually decided and whether the BMS confirmed it (GatewayState.authority),
so the FSM never claims control that is only intended.

IMPORTANT — this deliberately implements the partner's stated
preference (Sprint 1 feedback email, item 3): "continue executing
previously approved schedules and transition to a safe state if no
valid schedule is available." AUTONOMOUS here means "keep running
what was already approved" — it does NOT mean ML-generated dispatch.
If the team decides to keep the ML autonomous-fallback concept from
the original Sprint 1 proposal, that would plug in as an additional
input to _evaluate_disconnected_substate(), not a replacement of this
FSM.

Every transition is logged (Python logging) AND recorded to the
AuditLog (Requirement NF-03), so the full transition history is
reviewable after any test scenario, not just visible in a live
terminal.
"""

import asyncio
import logging
from enum import Enum

log = logging.getLogger("reliability")


class ReliabilityState(Enum):
    CONNECTED    = "CONNECTED"
    DERATED      = "DERATED"
    DISCONNECTED = "DISCONNECTED"
    AUTONOMOUS   = "AUTONOMOUS"
    SAFE_IDLE    = "SAFE_IDLE"


# Grid unreachable: which of these applies depends on the dispatch decision.
_UNREACHABLE = (ReliabilityState.DISCONNECTED, ReliabilityState.AUTONOMOUS, ReliabilityState.SAFE_IDLE)


class ReliabilityManager:
    """
    Owns the FSM. Observes grid-connectivity signals from IEEEClient
    and schedule-validity signals from Scheduler, and exposes the
    current reliability_state for other modules to consult.

    It does NOT make scheduling or Modbus decisions itself — it is a
    pure observer/state-tracker, matching the modularity goal (NF-05).
    """

    def __init__(self, config: dict, state, audit_log=None, event_bus=None,
                 derated_after_n_misses: int = 1,
                 disconnected_after_n_misses: int = 3):
        self.state = state  # GatewayState — provides seconds_since_grid_contact()
        self.offline_timeout = config["offline_timeout_sec"]
        self.audit_log = audit_log
        self.event_bus = event_bus  # optional — NF-05

        self.reliability_state = ReliabilityState.CONNECTED
        self.consecutive_missed_polls = 0
        self.derated_after_n_misses = derated_after_n_misses
        self.disconnected_after_n_misses = disconnected_after_n_misses

    # ── Inputs from IEEEClient ──────────────────────────────────

    def record_poll_success(self):
        self.consecutive_missed_polls = 0
        self._transition(ReliabilityState.CONNECTED)

    def record_poll_failure(self):
        self.consecutive_missed_polls += 1
        if self.consecutive_missed_polls >= self.disconnected_after_n_misses:
            self._evaluate_disconnected_substate()
        elif self.consecutive_missed_polls >= self.derated_after_n_misses:
            self._transition(ReliabilityState.DERATED)

    # ── Input from Scheduler (does a valid stored schedule exist?) ──

    def evaluate_with_schedule_status(self, has_valid_schedule: bool):
        """Re-evaluate once disconnected with an explicit answer to "is a
        valid approved schedule being executed?". Normal operation doesn't
        need this: record_poll_failure() reads the arbiter's decision. This
        decides AUTONOMOUS vs SAFE_IDLE — never an ML-generated decision in
        this design."""
        if self.reliability_state in _UNREACHABLE:
            self._evaluate_disconnected_substate(has_valid_schedule)

    # ── Input from the Scheduler, every tick ────────────────────

    def refresh(self):
        """Re-evaluate the grid-unreachable sub-state from the arbiter's
        latest decision (GatewayState.authority). The scheduler calls this
        whenever it publishes a decision, so AUTONOMOUS / SAFE_IDLE /
        DISCONNECTED change with the dispatch they describe instead of at
        the next failed poll, up to a poll interval later."""
        if self.reliability_state in _UNREACHABLE:
            self._evaluate_disconnected_substate()

    def _evaluate_disconnected_substate(self, has_valid_schedule: bool | None = None):
        """AUTONOMOUS while the arbiter runs an approved event and the BMS has
        confirmed its setpoint; SAFE_IDLE once, beyond the grace period, the
        arbiter holds 0 kW and the BMS has confirmed it; otherwise
        DISCONNECTED. An explicit has_valid_schedule stands in for the
        arbiter's decision (and is taken as confirmed)."""
        secs_offline = self.state.seconds_since_grid_contact()
        if has_valid_schedule is None:
            decision = getattr(self.state, "authority", None) or {}
            confirmed = bool(decision.get("confirmed"))
            # An approved event, or the utility's DefaultDERControl between events:
            # both are the utility's standing instructions, kept through the outage.
            approved = decision.get("authority") in ("approved_schedule", "default_control")
            running_schedule = approved and confirmed
            idle_confirmed = not approved and confirmed
        else:
            running_schedule = has_valid_schedule
            idle_confirmed = not has_valid_schedule

        if running_schedule:
            self._transition(ReliabilityState.AUTONOMOUS)
        elif secs_offline > self.offline_timeout and idle_confirmed:
            self._transition(ReliabilityState.SAFE_IDLE)
        else:
            self._transition(ReliabilityState.DISCONNECTED)

    # ── Internal ─────────────────────────────────────────────────

    def _transition(self, new_state: ReliabilityState):
        if new_state == self.reliability_state:
            return
        old_state = self.reliability_state
        self.reliability_state = new_state
        log.warning(f"Reliability state: {old_state.value} -> {new_state.value}")
        if self.audit_log:
            self.audit_log.record(
                event_type="state_transition",
                source="reliability_manager",
                detail=f"{old_state.value} -> {new_state.value}",
                outcome="ok",
            )
        if self.event_bus:
            # _transition() is called synchronously from record_poll_success/
            # failure, which in turn are called synchronously from
            # IEEEClient's async poll loop — so a running loop always
            # exists in normal operation. The try/except only guards
            # direct, non-async unit-test usage of this class.
            try:
                asyncio.get_running_loop().create_task(
                    self.event_bus.publish("state_change", {
                        "old_state": old_state.value,
                        "new_state": new_state.value,
                    })
                )
            except RuntimeError:
                pass