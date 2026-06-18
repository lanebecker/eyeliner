"""A minimal typed synchronous signal/observer.

One implementation with a single, documented delivery policy, replacing the two
hand-rolled observer loops that had divergent exception handling — PlayerState
guarded each callback, SilenceDetector did not, so one throwing listener killed
delivery to the rest mid-event (A-11).

Contract:
  - Listeners are invoked **synchronously**, in registration order, on the
    caller's thread (the asyncio event-loop thread in this app).
  - Listeners **must not block** — they run inline in the emitter's call stack.
  - A listener that raises is **logged and skipped**; it never prevents
    delivery to the remaining listeners (log-and-continue).
"""
import logging
from typing import Callable, Generic, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class Signal(Generic[T]):
    """A typed list of listeners notified with a single value of type T."""

    def __init__(self, name: str = ""):
        self._name = name
        self._listeners: list[Callable[[T], None]] = []

    def connect(self, listener: Callable[[T], None]) -> None:
        """Register a listener.  Listeners are called in registration order."""
        self._listeners.append(listener)

    def emit(self, value: T) -> None:
        """Notify every listener with `value`, log-and-continuing on errors."""
        for listener in self._listeners:
            try:
                listener(value)
            except Exception as e:
                log.error(f"Signal({self._name}) listener error: {e}")

    def __len__(self) -> int:
        return len(self._listeners)
