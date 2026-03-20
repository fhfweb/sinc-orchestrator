from services.streaming.core.config import env_get
"""
Outbound Webhook Worker
=======================
Polls the `outbound_webhooks` table for pending deliveries and POSTs them
to the tenant's configured webhook_url with HMAC-SHA256 signature.

Retry schedule (exponential backoff):
  attempt 1 →  30s
  attempt 2 →  2m
  attempt 3 →  10m
  attempt 4 →  1h
  attempt 5+ → permanent failure (status = 'failed')

Signature header: X-Sinc-Signature: sha256=<hmac>
Payload header:   Content-Type: application/json

Usage:
    python webhook_worker.py              # run forever, poll every 5s
    python webhook_worker.py --interval 10
"""

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
import httpx
from services.http_client import create_sync_resilient_client
from services.streaming.core.config import DB_CONFIG

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────

MAX_ATTEMPTS = int(env_get("WEBHOOK_MAX_ATTEMPTS", default="5"))
DELIVERY_TIMEOUT = int(env_get("WEBHOOK_TIMEOUT_S", default="10"))

_BACKOFF_SECONDS = [30, 120, 600, 3600]   # per attempt index (0-based)


# ──────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────

def _db():
    from services.streaming.core.db import db
    return db(bypass_rls=True)


def _fetch_pending(limit: int = 50) -> list[dict]:
    """Fetch pending/retryable webhooks whose next_attempt_at has passed."""
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT w.id, w.tenant_id, w.event_type, w.payload,
                           w.target_url, w.attempts, t.webhook_secret
                    FROM outbound_webhooks w
                    LEFT JOIN tenants t ON t.id = w.tenant_id
                    WHERE w.status IN ('pending', 'failed')
                      AND w.attempts < %s
                      AND w.next_attempt_at <= NOW()
                    ORDER BY w.next_attempt_at ASC
                    LIMIT %s
                    FOR UPDATE OF w SKIP LOCKED
                """, (MAX_ATTEMPTS, limit))
                return cur.fetchall()
    except Exception as exc:
        print(f"[webhook-worker] DB fetch error: {exc}")
        return []


def _mark_delivered(webhook_id: int):
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE outbound_webhooks
                    SET status = 'delivered', delivered_at = NOW()
                    WHERE id = %s
                """, (webhook_id,))
                conn.commit()
    except Exception as exc:
        print(f"[webhook-worker] mark delivered error: {exc}")


def _mark_failed(webhook_id: int, attempts: int, error: str):
    """Schedule retry or mark permanent failure."""
    next_attempt_delta = _BACKOFF_SECONDS[min(attempts, len(_BACKOFF_SECONDS) - 1)]
    permanent = attempts + 1 >= MAX_ATTEMPTS
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE outbound_webhooks
                    SET attempts = attempts + 1,
                        last_error = %s,
                        status = %s,
                        next_attempt_at = NOW() + (%s || ' seconds')::INTERVAL
                    WHERE id = %s
                """, (
                    error[:500],
                    "failed" if permanent else "pending",
                    str(next_attempt_delta),
                    webhook_id,
                ))
                conn.commit()
    except Exception as exc:
        print(f"[webhook-worker] mark failed error: {exc}")


# ──────────────────────────────────────────────
# DELIVERY
# ──────────────────────────────────────────────

def _sign_payload(body_bytes: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature."""
    if not secret:
        return ""
    sig = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def _deliver(webhook: dict) -> tuple[bool, str]:
    """
    POST the webhook to target_url.
    Returns (success: bool, error_msg: str).
    """
    url     = webhook["target_url"]
    payload = webhook["payload"]
    secret  = webhook.get("webhook_secret") or ""

    envelope = {
        "id":         webhook["id"],
        "event":      webhook["event_type"],
        "tenant_id":  webhook["tenant_id"],
        "payload":    payload if isinstance(payload, dict) else json.loads(payload),
        "timestamp":  datetime.now(timezone.utc).isoformat(),
    }
    body_bytes = json.dumps(envelope, ensure_ascii=False).encode()
    signature  = _sign_payload(body_bytes, secret)

    headers = {"Content-Type": "application/json", "User-Agent": "SINC-Orchestrator/4.0"}
    if signature:
        headers["X-Sinc-Signature"] = signature

    try:
        with create_sync_resilient_client(
            service_name="webhook-worker",
            timeout=DELIVERY_TIMEOUT,
            headers=headers,
        ) as client:
            resp = client.post(url, content=body_bytes)
            resp.raise_for_status()
            return True, ""
    except httpx.HTTPStatusError as e:
        return False, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except Exception as exc:
        return False, str(exc)


# ──────────────────────────────────────────────
# MAIN LOOP
# ──────────────────────────────────────────────

def run(interval: int = 5):
    print(f"[webhook-worker] starting — poll every {interval}s, max {MAX_ATTEMPTS} attempts")
    while True:
        rows = _fetch_pending()
        for webhook in rows:
            wid      = webhook["id"]
            attempts = webhook["attempts"]
            success, error = _deliver(webhook)
            if success:
                print(f"[webhook-worker] delivered #{wid}  event={webhook['event_type']}")
                _mark_delivered(wid)
            else:
                print(f"[webhook-worker] failed #{wid}  attempt={attempts+1}  error={error}")
                _mark_failed(wid, attempts, error)
        time.sleep(interval)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=5, help="Poll interval in seconds")
    args = parser.parse_args()
    run(args.interval)
