"""Daily Suggest Fix quota.

The quota is global per user (not per section) and resets by calendar date in
UTC. A new date naturally writes to a new document, so no reset job exists.

    users/{uid}/usage/{YYYY-MM-DD}  ->  { suggest_fix: 4, updated_at: ... }

Consumption must be atomic: two concurrent requests that both read `4` and both
write `5` would hand out a free credit. Every mutation therefore runs inside a
Firestore transaction, and only a committed transaction consumes a credit.
"""

from datetime import datetime, timezone

from google.cloud import firestore

import errors
import plans

QUOTA_FIELD = "suggest_fix"


def today_key() -> str:
    """Current date in UTC. The reset boundary for every user."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _usage_ref(db, uid: str, date_key: str):
    return db.collection("users").document(uid).collection("usage").document(date_key)


def _used(snapshot) -> int:
    if not snapshot.exists:
        return 0
    try:
        return int((snapshot.to_dict() or {}).get(QUOTA_FIELD, 0))
    except (TypeError, ValueError):
        return 0


def get_plan(db, uid: str) -> str:
    snap = db.collection("users").document(uid).get()
    return plans.resolve_plan(snap.to_dict() if snap.exists else None)


def get_status(db, uid: str) -> dict:
    """Read-only view for the quota indicator. Consumes nothing."""
    plan = get_plan(db, uid)
    limit = plans.daily_limit(plan)
    used = _used(_usage_ref(db, uid, today_key()).get())
    return {
        "plan": plan,
        "daily_limit": limit,
        "used_today": min(used, limit),
        "remaining_credits": max(0, limit - used),
    }


def consume(db, uid: str, plan: str) -> int:
    """Atomically consume one credit. Returns credits remaining after the spend.

    Raises SUGGEST_FIX_QUOTA_REACHED if the user is already at their limit.
    """
    limit = plans.daily_limit(plan)
    usage_ref = _usage_ref(db, uid, today_key())

    @firestore.transactional
    def _txn(transaction):
        used = _used(usage_ref.get(transaction=transaction))
        if used >= limit:
            return None
        transaction.set(
            usage_ref,
            {QUOTA_FIELD: used + 1, "updated_at": datetime.now(timezone.utc).isoformat()},
            merge=True,
        )
        return limit - (used + 1)

    remaining = _txn(db.transaction())
    if remaining is None:
        raise errors.quota_reached(
            f"You've reached your daily Suggest a Fix limit ({limit} today)."
        )
    return remaining


def refund(db, uid: str, plan: str) -> int:
    """Give a credit back after a failure that wasn't the user's fault.

    Only called when the LLM never produced a valid suggestion. Clamped at zero
    so a double refund can't mint credits.
    """
    limit = plans.daily_limit(plan)
    usage_ref = _usage_ref(db, uid, today_key())

    @firestore.transactional
    def _txn(transaction):
        used = _used(usage_ref.get(transaction=transaction))
        restored = max(0, used - 1)
        transaction.set(
            usage_ref,
            {QUOTA_FIELD: restored, "updated_at": datetime.now(timezone.utc).isoformat()},
            merge=True,
        )
        return max(0, limit - restored)

    try:
        return _txn(db.transaction())
    except Exception:
        # A failed refund must never mask the original error the caller is
        # already raising.
        return 0
