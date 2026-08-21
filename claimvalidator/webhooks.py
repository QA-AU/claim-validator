"""Best-effort webhook delivery — same guarded philosophy as `RunTracker`:
a delivery failure must never take down the job it's reporting on."""

import logging
from typing import Any, Dict

import requests

logger = logging.getLogger(__name__)

TIMEOUT_S = 10
MAX_ATTEMPTS = 3


def deliver(url: str, payload: Dict[str, Any]) -> bool:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.post(url, json=payload, timeout=TIMEOUT_S)
            if response.ok:
                return True
            logger.warning(f"Webhook {url} returned {response.status_code} (attempt {attempt})")
        except requests.RequestException as e:
            logger.warning(f"Webhook {url} failed (attempt {attempt}): {e}")
    logger.error(f"Webhook {url} gave up after {MAX_ATTEMPTS} attempts")
    return False
