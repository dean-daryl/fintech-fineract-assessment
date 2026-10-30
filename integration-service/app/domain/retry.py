"""Deferred retry backoff. Pure functions, no I/O."""

import random
from datetime import timedelta

RETRY_BASE_SECONDS = 5
RETRY_CAP_SECONDS = 300


def retry_delay(attempts: int) -> timedelta:
    seconds = min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * 2 ** max(attempts - 1, 0))
    return timedelta(seconds=seconds + random.uniform(0, 1))
