"""Plan definitions and the daily Suggest Fix limit for each.

free  : 2 credits/day
basic : 6 credits/day  (INR 190/month)
pro   : 15 credits/day (INR 250/month)

Limits are env-overridable so pricing experiments don't need a deploy.
"""

import os

from dotenv import load_dotenv

load_dotenv()

PLAN_FREE = "free"
PLAN_BASIC = "basic"
PLAN_PRO = "pro"

PLAN_LIMITS = {
    PLAN_FREE: int(os.getenv("SUGGEST_FIX_LIMIT_FREE", "2")),
    PLAN_BASIC: int(os.getenv("SUGGEST_FIX_LIMIT_BASIC", "6")),
    PLAN_PRO: int(os.getenv("SUGGEST_FIX_LIMIT_PRO", "15")),
}

# A subscription only entitles a user to its plan while it is in one of these
# states. Anything else (halted, cancelled, expired, paused) falls back to free.
# "authenticated" is deliberately excluded: the mandate exists but the first
# payment has not cleared yet.
ACTIVE_SUBSCRIPTION_STATES = {"active"}


def resolve_plan(user_data: dict | None) -> str:
    """Effective plan for a user document.

    Users created before billing existed have no `plan` field at all, so a
    missing value means free rather than an error.
    """
    data = user_data or {}
    plan = (data.get("plan") or PLAN_FREE).lower()
    if plan not in PLAN_LIMITS:
        return PLAN_FREE
    if plan != PLAN_FREE:
        status = (data.get("subscription_status") or "").lower()
        if status not in ACTIVE_SUBSCRIPTION_STATES:
            return PLAN_FREE
    return plan


def daily_limit(plan: str) -> int:
    return PLAN_LIMITS.get(plan, PLAN_LIMITS[PLAN_FREE])
