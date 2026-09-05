from fastapi import FastAPI, UploadFile, File, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import pdfplumber
import google.generativeai as genai
import os
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi.responses import RedirectResponse
import httpx
from dotenv import load_dotenv
import os
import secrets
from datetime import datetime, timedelta, timezone
from firebase_config import db
from google.cloud import firestore
from firebase_admin import auth as admin_auth
import errors
from auth import require_uid
from fastapi import Depends
import suggestions
import payments
import quota
import plans

load_dotenv()

api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
genai.configure(api_key=api_key)
model = genai.GenerativeModel("gemini-2.5-flash")

app = FastAPI()


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
        "https://www.portflow.co.in",
        "https://portflow.co.in",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


#Rate limiter setup.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
# define the handler first
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"error": "Too many requests. Try again in a minute."})
app.add_exception_handler(RateLimitExceeded, rate_limit_handler)

# Suggest a Fix / payments error codes -> JSON bodies the frontend branches on
app.add_exception_handler(errors.AppError, errors.app_error_handler)
app.include_router(suggestions.router)
app.include_router(payments.router)

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://www.portflow.co.in")
ADMIN_SECRET = os.getenv("ADMIN_SECRET")
token = ''

@app.post("/auth/github/start")
async def github_start(uid: str = Depends(require_uid)):
    """Begin GitHub OAuth for the *authenticated* user.

    Replaces a GET that took ?uid= from the query string, which let anyone
    start a flow against someone else's account: authorise with your own
    GitHub and you overwrote the victim's stored token and username.

    A POST rather than a redirect because a top-level browser navigation
    cannot carry an Authorization header - the frontend calls this, then
    navigates to the URL it returns.
    """
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise HTTPException(
            status_code=503,
            detail="GitHub sync is not configured on the server.",
        )

    state = secrets.token_urlsafe(32)
    # Firestore rather than a process dictionary: Render restarts between the
    # redirect out and the callback back would otherwise lose the state, and
    # an in-memory dict grows without bound.
    db.collection("oauth_states").document(state).set({
        "uid": uid,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    return {
        "authorize_url": (
            "https://github.com/login/oauth/authorize"
            f"?client_id={GITHUB_CLIENT_ID}"
            f"&state={state}"
            "&scope=read:user"
        )
    }

@app.get("/auth/github/callback")
async def github_callback(code: str, state:str):

    state_ref = db.collection("oauth_states").document(state)
    state_doc = state_ref.get()
    if not state_doc.exists:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    state_data = state_doc.to_dict() or {}
    uid = state_data.get("uid")
    # Single use, whatever happens next.
    state_ref.delete()

    if not uid:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    started = state_data.get("created_at")
    if started:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(started)
        if age > timedelta(minutes=15):
            raise HTTPException(status_code=400, detail="This sign-in link expired. Try again.")
    
    async with httpx.AsyncClient() as client:

        token_response = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
            },
        )

        access_token = token_response.json()["access_token"]

        user_response = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
        )

        github_user = user_response.json()

    # The token lives in a collection no browser can read. It used to sit on
    # users/{uid}, which the security rules make readable by any signed-in
    # user (the public-profile username lookup needs that), so every user's
    # GitHub token was readable by every other user.
    db.collection("github_tokens").document(uid).set({
        "access_token": access_token,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })

    db.collection("users").document(uid).set(
        {
            "github_username": github_user["login"],
            "github_id": github_user["id"],
            "github_connected": True,
            # Clear any token written by the previous version.
            "github_access_token": firestore.DELETE_FIELD,
        },
        merge=True,
    )

    return RedirectResponse(
        f"{FRONTEND_URL}/resume?template=sde&method=upload&github=connected"
    )



@app.get("/github/status")
async def github_status(uid: str = Depends(require_uid)):
    """Whether the authenticated user has connected GitHub.

    Took ?uid= before, so anyone could probe any account.
    """
    doc = db.collection("users").document(uid).get()
    data = doc.to_dict() or {}
    return {"connected": bool(data.get("github_connected", False))}

@app.delete("/admin/users/{uid}")
async def delete_user(uid: str, x_admin_secret: str = Header(None)):
    # Protect this destructive endpoint with a shared admin secret
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    # 1. Delete every Firestore doc for this user:
    #    the account doc plus each template-specific portfolio collection
    for coll in ("users", "sde", "bda", "custom"):
        db.collection(coll).document(uid).delete()

    # 2. Delete the Firebase Auth (Google SSO) account itself
    try:
        admin_auth.delete_user(uid)
    except admin_auth.UserNotFoundError:
        pass

    return {"deleted": uid}

@app.get("/admin/users")
async def list_users(x_admin_secret: str = Header(None)):
    """Every user with their plan and today's remaining AI credits.

    The admin UI used to read Firestore straight from the browser without
    signing in. Once the security rules land that stops working - and usage
    counters are owner-only regardless - so this goes through the Admin SDK,
    guarded by the shared admin secret.
    """
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    date_key = quota.today_key()
    docs = list(db.collection("users").stream())

    # One batched read for every usage counter. Fetching them one at a time is
    # a round trip per user, which is slow enough to hang the page.
    usage_refs = [
        db.collection("users").document(d.id).collection("usage").document(date_key)
        for d in docs
    ]
    used_by_uid = {}
    if usage_refs:
        for snap in db.get_all(usage_refs):
            if snap.exists:
                # parent of the usage doc's collection is the user document
                used_by_uid[snap.reference.parent.parent.id] = int(
                    (snap.to_dict() or {}).get("suggest_fix", 0)
                )

    users = []
    for doc in docs:
        data = doc.to_dict() or {}
        plan = plans.resolve_plan(data)
        limit = plans.daily_limit(plan)
        used = used_by_uid.get(doc.id, 0)

        users.append({
            "id": doc.id,
            "username": data.get("username"),
            "email": data.get("email"),
            "createdAt": (data.get("createdAt") or "").split("T")[0],
            "github_connected": bool(data.get("github_connected")),
            "plan": plan,
            "subscription_status": data.get("subscription_status"),
            "daily_limit": limit,
            "used_today": used,
            "remaining_credits": max(0, limit - used),
        })

    users.sort(key=lambda u: u.get("createdAt") or "", reverse=True)
    return {"date": date_key, "count": len(users), "users": users}


@app.delete("/admin/users/{uid}/portfolio")
async def delete_user_portfolio(uid: str, x_admin_secret: str = Header(None)):
    """Wipe a user's uploaded resume so the upload flow can be tested again.

    Leaves the account and their GitHub connection alone - this is the "start
    over from the resume upload" button, not a delete.
    """
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    cleared = []
    for template in ("sde", "bda", "custom"):
        ref = db.collection(template).document(uid)
        if ref.get().exists:
            ref.delete()
            cleared.append(template)
    return {"uid": uid, "cleared": cleared}


@app.delete("/admin/users/{uid}/github")
async def purge_user_github(uid: str, x_admin_secret: str = Header(None)):
    """Disconnect a user's GitHub so the OAuth flow can be tested again.

    Was done straight from the admin browser, which the security rules will
    refuse twice over: the admin app never signs in, and github_access_token
    is a backend-only field.
    """
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    db.collection("github_tokens").document(uid).delete()
    db.collection("users").document(uid).update({
        "github_connected": False,
        "github_access_token": firestore.DELETE_FIELD,
        "github_username": firestore.DELETE_FIELD,
        "github_id": firestore.DELETE_FIELD,
    })
    return {"uid": uid, "github_connected": False}


@app.post("/admin/users/{uid}/reset-credits")
async def reset_user_credits(uid: str, x_admin_secret: str = Header(None)):
    """Give a user their full daily Suggest a Fix allowance back.

    Deletes today's usage document rather than zeroing it - a missing document
    already means "nothing used today", which is the same path a new day takes.
    """
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    date_key = quota.today_key()
    usage_ref = (
        db.collection("users").document(uid).collection("usage").document(date_key)
    )
    existed = usage_ref.get().exists
    if existed:
        usage_ref.delete()

    status = quota.get_status(db, uid)
    return {"uid": uid, "date": date_key, "reset": existed, **status}


@app.post("/admin/rate-limit/reset")
async def reset_rate_limit(x_admin_secret: str = Header(None)):
    """Clear the /parse-resume rate limiter (1 request per minute per IP).

    Testing the upload flow repeatedly otherwise means waiting out the minute
    after every attempt. The limiter keeps its counters in memory in this
    process, so this clears every key rather than one - fine for a single
    instance, and the counters are lost on restart anyway.
    """
    if not ADMIN_SECRET or x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        cleared = limiter.limiter.storage.reset()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not reset limiter: {e}")
    return {"cleared_keys": cleared, "limit": "1/minute on /parse-resume"}


@app.get("/admin/debug")
async def admin_debug():
    # TEMPORARY: reports whether the backend sees ADMIN_SECRET (not its value)
    return {
        "admin_secret_set": bool(ADMIN_SECRET),
        "admin_secret_length": len(ADMIN_SECRET) if ADMIN_SECRET else 0,
    }

@app.get("/github/contributions")
async def github_contributions(uid: str = Depends(require_uid)):
    """The authenticated user's own contribution calendar.

    Took ?uid= before, which handed anyone's data to anyone who knew a uid.
    """
    token_doc = db.collection("github_tokens").document(uid).get()
    access_token = (token_doc.to_dict() or {}).get("access_token") if token_doc.exists else None

    if not access_token:
        # Users who connected before the token moved still have it on their
        # user document; migrate them across on first use.
        legacy = db.collection("users").document(uid).get().to_dict() or {}
        access_token = legacy.get("github_access_token")
        if access_token:
            db.collection("github_tokens").document(uid).set({
                "access_token": access_token,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            db.collection("users").document(uid).update(
                {"github_access_token": firestore.DELETE_FIELD}
            )

    if not access_token:
        raise HTTPException(status_code=404, detail="GitHub is not connected.")

    query = """
    query {
      viewer {
        contributionsCollection {
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                contributionCount
                date
                weekday
                color
              }
            }
          }
        }
      }
    }
    """
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.github.com/graphql",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            json={"query": query},
        )

    return response.json()

@app.post("/parse-resume")
@limiter.limit("1/minute")
async def parse_resume(request: Request, file: UploadFile = File(...)):
    import json, re, logging

    logger = logging.getLogger("parse-resume")

    try:
        with pdfplumber.open(file.file) as pdf:
            text = ""
            for page in pdf.pages:
                text += page.extract_text() or ""
        logger.info(f"PDF extracted: {len(text)} chars")
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to read PDF: {str(e)}")

    prompt = f"""
    Parse this resume text into JSON with exactly this structure:
    {{
      "name": "",
      "title": "",
      "bio": "",
      "email": "",
      "github": "",
      "linkedin": "",
      "experience": [{{"role": "", "company": "", "period": "", "description": ""}}],
      "education": [{{"degree": "", "institution": "", "period": "", "description": ""}}],
      "skills": [""],
      "projects": [{{"name": "", "description": ""}}]
    }}
    Return only valid JSON, no markdown, no explanation.
    Resume text:
    {text}
    """

    try:
        response = model.generate_content(prompt)
        logger.info("Gemini response received")
    except Exception as e:
        logger.error(f"Gemini API failed: {e}")
        raise HTTPException(status_code=429 if "quota" in str(e).lower() else 500, detail=f"AI parsing failed: {str(e)}")

    try:
        raw = response.text.strip()
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"^```\s*", "", raw)
        raw = re.sub(r"```$", "", raw).strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            raw = match.group(0)
        parsed = json.loads(raw)
        logger.info("JSON parsed successfully")
        return parsed
    except Exception as e:
        logger.error(f"JSON parsing failed: {e}, raw response: {raw[:200]}")
        raise HTTPException(status_code=500, detail=f"Failed to parse AI response: {str(e)}")
