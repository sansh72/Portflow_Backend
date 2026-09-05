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
from firebase_config import db
from firebase_admin import auth as admin_auth
import errors
import suggestions
import payments

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
oauth_state = {}
token = ''

@app.get("/auth/github/login")

async def github_login(uid:str):

    state = secrets.token_urlsafe(32)
    oauth_state[state] = uid

    github_url = (
        "https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&state={state}"
        "&scope=read:user"
    )
    return RedirectResponse(github_url)

@app.get("/auth/github/callback")
async def github_callback(code: str, state:str):

    uid = oauth_state.pop(state, None)
    if uid is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid OAuth state"
        )
    if uid is None:
        return {"error": "Invalid state"}
    
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

    print('uid', uid)
    print('token', access_token)

    db.collection("users").document(uid).set(
    {
        "github_access_token": access_token,
        "github_username": github_user["login"],
        "github_id": github_user["id"],
        "github_connected": True,
    },
    merge=True,
)
    

    doc = db.collection("users").document(uid).get()

    print(doc.exists)

    print(doc.to_dict())
    print('This is token', token)


    return RedirectResponse(
        f"{FRONTEND_URL}/resume?template=sde&method=upload&github=connected"
    )



@app.get("/github/status")
async def github_status(uid: str):
    doc = db.collection("users").document(uid).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="User not found")
    data = doc.to_dict()
    return {
        "connected": data.get("github_connected", False)
    }

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

@app.get("/admin/debug")
async def admin_debug():
    # TEMPORARY: reports whether the backend sees ADMIN_SECRET (not its value)
    return {
        "admin_secret_set": bool(ADMIN_SECRET),
        "admin_secret_length": len(ADMIN_SECRET) if ADMIN_SECRET else 0,
    }

@app.get("/github/contributions")
async def github_contributions(uid:str):

    doc = db.collection("users").document(uid).get()
    if not doc.exists:
        return {"error": "User not found"}

    data = doc.to_dict()
    access_token = data["github_access_token"]

    if not access_token:
        raise HTTPException(
            status_code=404,
            detail="No Access Token Found"
        )

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
