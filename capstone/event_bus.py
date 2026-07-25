"""
capstone/event_bus.py
Internal Event Bus (Requirement NF-05)

"Independent modules communicating via asyncio.Queue event bus; each
module testable in isolation." — Sprint 1 Report, Table 2.

Scope note: this bus carries the fire-and-forget, one-directional
notifications the architecture diagram (Figure 1) describes — state
changes and alerts. It deliberately does NOT carry the scheduler
dispatch path (IEEEClient -> Scheduler -> ModbusDriver): that path
needs an immediate, synchronous success/failure return value to drive
retry logic (see scheduler.py's executed=-1/0/1 states), which a
queue-based publish/subscribe model can't provide without extra
request/response plumbing. Routing only the observational, decoupled
messages through the bus — while keeping the correctness-critical
command path as direct calls — matches how the diagram itself
distinguishes "typed internal messages: schedule commands, state
changes, ML recommendations, alerts" as being carried on the bus for
visibility, from the actual command execution, which still has to be a
real function call with a real return value.
"""

import asyncio
import time


class EventBus:
    """A minimal pub/sub bus: each subscriber gets its own
    asyncio.Queue and receives every published message, independent of
    the others — so a slow or crashed subscriber can never block
    publishers or other subscribers."""

    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    async def publish(self, topic: str, payload: dict):
        message = {"topic": topic, "payload": payload, "ts": time.time()}
        for queue in self._subscribers:
            await queue.put(message)
