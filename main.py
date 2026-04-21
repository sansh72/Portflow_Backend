from fastapi import FastAPI, UploadFile, File, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import pdfplumber
import google.generativeai as genai
import os
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

api_key = os.environ.get("GEMINI_API_KEY")
genai.configure(api_key=api_key)
model = genai.GenerativeModel("gemini-2.5-flash")

app = FastAPI()


#Rate limiter setup.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
# define the handler first
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"error": "Too many requests. Try again in a minute."})
app.add_exception_handler(RateLimitExceeded, rate_limit_handler)



app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.portflow.co.in"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
