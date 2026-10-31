"""On-demand failure injection for the Part J scenarios. Test-only switches held in memory, never business state."""

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

log = logging.getLogger(__name__)


class FaultPoint(StrEnum):
    FINERACT_DOWN = "FINERACT_DOWN"
    FINERACT_TIMEOUT_AFTER_SEND = "FINERACT_TIMEOUT_AFTER_SEND"
    ERP_TIMEOUT_AFTER_WRITE = "ERP_TIMEOUT_AFTER_WRITE"
    CRASH_BEFORE_ERP = "CRASH_BEFORE_ERP"


@dataclass
class Fault:
    point: FaultPoint
    external_reference: str
    remaining: int


def _exit_process() -> None:
    os._exit(1)


class FaultInjector:
    def __init__(self, enabled: bool, crash: Callable[[], None] = _exit_process) -> None:
        self.enabled = enabled
        self._crash = crash
        self._faults: list[Fault] = []
        self._lock = threading.Lock()

    def arm(self, point: FaultPoint, external_reference: str, times: int) -> Fault:
        fault = Fault(point, external_reference, times)
        with self._lock:
            self._faults = [f for f in self._faults if (f.point, f.external_reference) != (point, external_reference)]
            self._faults.append(fault)
        log.warning("Fault armed: %s x%d", point, times, extra={"external_reference": external_reference})
        return fault

    def clear(self) -> None:
        with self._lock:
            self._faults = []

    def armed(self) -> list[dict[str, Any]]:
        with self._lock:
            return [asdict(f) for f in self._faults]

    def fires(self, point: FaultPoint, external_reference: str) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            fault = next((f for f in self._faults if f.point is point and f.external_reference == external_reference), None)
            if fault is None:
                return False
            fault.remaining -= 1
            if fault.remaining <= 0:
                self._faults.remove(fault)
        log.warning("Fault fired: %s", point, extra={"external_reference": external_reference})
        return True

    def crash_if_armed(self, external_reference: str) -> None:
        if self.fires(FaultPoint.CRASH_BEFORE_ERP, external_reference):
            log.critical("Injected crash: the process exits now", extra={"external_reference": external_reference})
            logging.shutdown()
            self._crash()
