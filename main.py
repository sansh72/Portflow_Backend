from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
import pdfplumber
import google.generativeai as genai
import os

api_key = os.environ.get("GEMINI_API_KEY")
genai.configure(api_key=api_key)
model = genai.GenerativeModel("gemini-2.5-flash")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/parse-resume")
async def parse_resume(file: UploadFile = File(...)):
    with pdfplumber.open(file.file) as pdf:
        text = ""
        for page in pdf.pages:
            text += page.extract_text() or ""

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

    response = model.generate_content(prompt)
    import json, re
    raw = response.text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"^```\s*", "", raw)
    raw = re.sub(r"```$", "", raw).strip()
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        raw = match.group(0)
    parsed = json.loads(raw)
    return parsed
