"""Signed job-completion callbacks (§5.8).

A background research run can take ten minutes. Polling `GET /v1/jobs/{id}`
until then works but wastes both sides' time; a webhook lets the client be told.

Three properties matter, and each one is a rule here:

* **the receiver must be able to trust the payload.** Every POST carries
  ``X-Webhook-Signature: sha256=<hex>``, an HMAC-SHA256 of the exact request body
  under ``WEBHOOK_SECRET``. Without a secret configured the endpoint refuses to
  accept a `webhook_url` at all (see the route) rather than sending something
  unauthenticated that a receiver might act on.
* **the callback target is attacker-controlled.** It is a URL supplied by a
  client that the *server* then fetches, which is textbook SSRF — so it goes
  through the same `UrlGuard` as any crawl target, at request time.
* **a webhook must never fail the job.** Every error here is swallowed and
  logged. The run's result is already stored; a client that misses its callback
  can still fetch it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger("unified_search_api.webhook")

SIGNATURE_HEADER = "X-Webhook-Signature"


def sign(body: bytes, secret: str) -> str:
    """``sha256=<hex>`` over the raw body, the form receivers expect."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def deliver(
    url: str,
    payload: dict[str, Any],
    *,
    secret: str,
    timeout_seconds: float = 10.0,
) -> bool:
    """POST ``payload`` to ``url``, signed. Returns whether it was accepted.

    Never raises: a dead, slow or hostile webhook endpoint is the caller's
    problem, not a reason to mark a completed run as failed.
    """
    if not secret:  # pragma: no cover - the route rejects this earlier
        logger.warning("Webhook not sent: WEBHOOK_SECRET is not configured")
        return False
    try:
        body = json.dumps(payload, default=str).encode()
    except (TypeError, ValueError):
        logger.exception("Webhook payload is not JSON-serializable; not sent")
        return False

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    SIGNATURE_HEADER: sign(body, secret),
                },
            )
        if response.status_code >= 400:
            logger.warning("Webhook rejected by the receiver: HTTP %s", response.status_code)
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - the job outcome does not depend on this
        logger.warning("Webhook delivery failed: %s", exc)
        return False
