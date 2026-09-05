"""Razorpay subscriptions -> Firestore plan state.

Plans: basic INR 190/month (6 credits/day), pro INR 250/month (15 credits/day).

The frontend never sets `plan`. It asks the backend to create a subscription,
opens Razorpay checkout, and the *webhook* is what actually grants the plan.
The post-payment redirect is UX only.
"""

import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Request
from dotenv import load_dotenv
from pydantic import BaseModel, Field

import errors
import plans
from auth import require_uid
from firebase_config import db

load_dotenv()

logger = logging.getLogger("payments")

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")

# Razorpay plan ids, from the Razorpay dashboard.
PLAN_IDS = {
    plans.PLAN_BASIC: os.getenv("RAZORPAY_PLAN_BASIC"),
    plans.PLAN_PRO: os.getenv("RAZORPAY_PLAN_PRO"),
}
PLAN_BY_RAZORPAY_ID = {v: k for k, v in PLAN_IDS.items() if v}

# Subscription lifecycle events we act on.
_GRANTS = {"subscription.activated", "subscription.charged", "subscription.resumed"}
_REVOKES = {
    "subscription.cancelled",
    "subscription.completed",
    "subscription.expired",
    "subscription.halted",
    "subscription.paused",
}


class SubscribeRequest(BaseModel):
    plan: str = Field(..., max_length=16)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client():
    if not (RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET):
        raise errors.AppError(503, "PAYMENTS_UNAVAILABLE", "Payments are not configured.")
    try:
        import razorpay
    except ImportError:
        logger.error("razorpay package is not installed")
        raise errors.AppError(503, "PAYMENTS_UNAVAILABLE", "Payments are not configured.")
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


@router.get("/config")
async def payments_config():
    """What the frontend needs to render pricing and open checkout."""
    return {
        "key_id": RAZORPAY_KEY_ID,
        "enabled": bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET),
        "plans": [
            {
                "plan": plans.PLAN_FREE,
                "price_inr": 0,
                "daily_credits": plans.PLAN_LIMITS[plans.PLAN_FREE],
            },
            {
                "plan": plans.PLAN_BASIC,
                "price_inr": 190,
                "daily_credits": plans.PLAN_LIMITS[plans.PLAN_BASIC],
            },
            {
                "plan": plans.PLAN_PRO,
                "price_inr": 250,
                "daily_credits": plans.PLAN_LIMITS[plans.PLAN_PRO],
            },
        ],
    }


@router.post("/subscription")
async def create_subscription(body: SubscribeRequest, uid: str = Depends(require_uid)):
    """Create a Razorpay subscription for this user and return its id for checkout."""
    plan = body.plan.lower()
    if plan not in PLAN_IDS:
        raise errors.AppError(400, "INVALID_PLAN", f"'{body.plan}' is not a purchasable plan.")

    razorpay_plan_id = PLAN_IDS[plan]
    if not razorpay_plan_id:
        raise errors.AppError(503, "PAYMENTS_UNAVAILABLE", "This plan is not available yet.")

    try:
        subscription = _client().subscription.create({
            "plan_id": razorpay_plan_id,
            "total_count": 12,
            "customer_notify": 1,
            # The webhook reads this back to know who paid.
            "notes": {"uid": uid, "plan": plan},
        })
    except errors.AppError:
        raise
    except Exception as e:
        logger.error("Razorpay subscription create failed: %s", e)
        raise errors.AppError(502, "PAYMENT_PROVIDER_ERROR", "Could not start checkout. Please try again.")

    db.collection("users").document(uid).set(
        {"razorpay_subscription_id": subscription["id"], "pending_plan": plan}, merge=True
    )
    return {"subscription_id": subscription["id"], "key_id": RAZORPAY_KEY_ID, "plan": plan}


def _verify_signature(raw_body: bytes, signature: str) -> None:
    if not RAZORPAY_WEBHOOK_SECRET:
        logger.error("RAZORPAY_WEBHOOK_SECRET is not set; rejecting webhook")
        raise errors.forbidden("Webhook not configured.")
    if not signature:
        raise errors.forbidden("Missing webhook signature.")
    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        logger.warning("Rejected Razorpay webhook with a bad signature")
        raise errors.forbidden("Invalid webhook signature.")


@router.post("/razorpay/webhook")
async def razorpay_webhook(
    request: Request, x_razorpay_signature: str = Header(None, alias="X-Razorpay-Signature")
):
    """Authoritative source of subscription state. Signature-verified."""
    raw = await request.body()
    _verify_signature(raw, x_razorpay_signature)

    try:
        payload = await request.json()
    except Exception:
        raise errors.AppError(400, "INVALID_PAYLOAD", "Malformed webhook body.")

    event = payload.get("event")
    subscription = (
        (payload.get("payload") or {}).get("subscription", {}).get("entity") or {}
    )
    notes = subscription.get("notes") or {}
    uid = notes.get("uid")
    subscription_id = subscription.get("id")

    if not uid:
        logger.warning("Razorpay webhook %s had no uid in notes; ignoring", event)
        return {"status": "ignored"}

    # Razorpay retries on non-2xx, so the same event can arrive more than once.
    event_id = payload.get("id") or f"{event}:{subscription_id}:{subscription.get('current_start')}"
    event_ref = db.collection("payment_events").document(str(event_id))
    if event_ref.get().exists:
        return {"status": "duplicate"}

    user_ref = db.collection("users").document(uid)

    if event in _GRANTS:
        plan = PLAN_BY_RAZORPAY_ID.get(subscription.get("plan_id")) or notes.get("plan")
        if plan not in plans.PLAN_LIMITS or plan == plans.PLAN_FREE:
            logger.warning("Razorpay webhook %s had unknown plan %s", event, plan)
            return {"status": "ignored"}
        user_ref.set(
            {
                "plan": plan,
                "subscription_status": "active",
                "razorpay_subscription_id": subscription_id,
                "subscription_updated_at": _now(),
                "pending_plan": None,
            },
            merge=True,
        )
    elif event in _REVOKES:
        # Keep the plan name for history; resolve_plan() demotes to free as
        # soon as the status stops being active.
        user_ref.set(
            {
                "subscription_status": event.split(".", 1)[1],
                "subscription_updated_at": _now(),
            },
            merge=True,
        )
    else:
        return {"status": "ignored"}

    event_ref.set({"event": event, "uid": uid, "processed_at": _now()})
    return {"status": "ok"}
