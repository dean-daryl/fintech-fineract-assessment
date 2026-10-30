"""Idempotency rules for repeated requests. Pure functions, no I/O."""

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from app.domain.states import OperationStatus, is_finished


class ReplayKind(StrEnum):
    STORED_RESULT = "STORED_RESULT"
    IN_PROGRESS = "IN_PROGRESS"
    PAYLOAD_CONFLICT = "PAYLOAD_CONFLICT"


def payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def replay_kind(status: OperationStatus, stored_hash: str, request_hash: str) -> ReplayKind:
    if stored_hash != request_hash:
        return ReplayKind.PAYLOAD_CONFLICT
    if is_finished(status):
        return ReplayKind.STORED_RESULT
    return ReplayKind.IN_PROGRESS
