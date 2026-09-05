"""Firebase ID token verification.

The UID is *always* derived from the verified token, never from the request
body or query string. Existing endpoints still take `uid` as a parameter;
new endpoints must depend on `require_uid` instead.
"""

from fastapi import Depends, Header
from firebase_admin import auth as admin_auth

import errors


async def require_uid(authorization: str = Header(None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise errors.unauthenticated("Missing authentication token.")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise errors.unauthenticated("Missing authentication token.")

    try:
        decoded = admin_auth.verify_id_token(token)
    except admin_auth.ExpiredIdTokenError:
        raise errors.unauthenticated("Your session expired. Please sign in again.")
    except Exception:
        # Covers revoked / malformed / wrong-project tokens. Never leak the
        # provider's message.
        raise errors.unauthenticated("Invalid authentication token.")

    uid = decoded.get("uid")
    if not uid:
        raise errors.unauthenticated("Invalid authentication token.")
    return uid


CurrentUid = Depends(require_uid)
