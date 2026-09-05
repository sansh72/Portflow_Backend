"""Gemini calls for Suggest a Fix, plus the server-owned prompt vocabulary.

Two distinct calls:
  analyze() - explains what could be improved. Must NOT rewrite the section.
  rewrite() - applies one tag's instruction and returns replacement text.

The frontend only ever sends a tag identifier. Instructions live here, so the
frontend cannot inject arbitrary prompts.
"""

import json
import logging
import os
import re

import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

import errors

logger = logging.getLogger("suggest-fix.llm")

_api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
if _api_key:
    genai.configure(api_key=_api_key)
_model = genai.GenerativeModel(os.getenv("SUGGEST_FIX_MODEL", "gemini-2.5-flash"))

ALLOWED_TAGS = {
    "make_concise",
    "more_professional",
    "highlight_achievements",
    "more_technical",
    "more_engaging",
    "remove_repetition",
    "rewrite_completely",
}

TAG_INSTRUCTIONS = {
    "make_concise":
        "Make the section more concise while preserving important information.",
    "more_professional":
        "Rewrite the section in a polished and professional tone.",
    "highlight_achievements":
        "Rewrite the section to emphasize concrete achievements, outcomes, and impact.",
    "more_technical":
        "Make the section more technically detailed while remaining readable.",
    "more_engaging":
        "Make the section more engaging and compelling while preserving its meaning.",
    "remove_repetition":
        "Remove redundant or repetitive statements while preserving important information.",
    "rewrite_completely":
        "Rewrite the section from scratch while preserving factual meaning and important information.",
}

# Human-readable labels for the tag chips, so the frontend doesn't hardcode them.
TAG_LABELS = {
    "make_concise": "More concise",
    "more_professional": "More professional",
    "highlight_achievements": "Highlight achievements",
    "more_technical": "More technical",
    "more_engaging": "More engaging",
    "remove_repetition": "Remove repetition",
    "rewrite_completely": "Rewrite completely",
}

MIN_TAGS = 6
MAX_TAGS = 7

_ANALYSIS_PROMPT = """You are reviewing one section of a personal portfolio.

Explain what could be improved about it. Do NOT rewrite the section, do not
produce an improved version, and do not quote a replacement. Two or three
sentences, addressed to the author, plain text only.

Then choose between {min_tags} and {max_tags} improvement tags from exactly this list:
{tags}

Return only valid JSON, no markdown fences, in this shape:
{{"analysis": "...", "suggested_tags": ["tag_one", "tag_two"]}}

Section type: {section_label}
Section content:
{content}
"""

_REWRITE_PROMPT = """You are editing one section of a personal portfolio.

Instruction: {instruction}

Rules:
- Preserve factual meaning. Never invent employers, dates, metrics, or credentials.
- Return only the rewritten section text. No preamble, no markdown, no quotes,
  no commentary.
- Keep it roughly the same length unless the instruction implies otherwise.

Section type: {section_label}
Current content:
{content}
"""


def _generate(prompt: str) -> str:
    try:
        response = _model.generate_content(prompt)
        text = (response.text or "").strip()
    except Exception as e:
        logger.error("Gemini call failed: %s", e)
        raise errors.llm_error()
    if not text:
        logger.error("Gemini returned an empty response")
        raise errors.invalid_llm_response()
    return text


def _strip_fences(raw: str) -> str:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"```$", "", raw).strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    return match.group(0) if match else raw


def analyze(content: str, section_label: str) -> dict:
    """Return {"analysis": str, "suggested_tags": [str]} - validated, never trusted raw."""
    prompt = _ANALYSIS_PROMPT.format(
        min_tags=MIN_TAGS,
        max_tags=MAX_TAGS,
        tags="\n".join(f"- {t}" for t in sorted(ALLOWED_TAGS)),
        section_label=section_label,
        content=content,
    )
    raw = _generate(prompt)

    try:
        parsed = json.loads(_strip_fences(raw))
    except (json.JSONDecodeError, ValueError):
        logger.error("Gemini analysis was not JSON: %s", raw[:200])
        raise errors.invalid_llm_response()

    if not isinstance(parsed, dict):
        raise errors.invalid_llm_response()

    analysis = parsed.get("analysis")
    if not isinstance(analysis, str) or not analysis.strip():
        raise errors.invalid_llm_response()

    raw_tags = parsed.get("suggested_tags")
    if not isinstance(raw_tags, list):
        raise errors.invalid_llm_response()

    # Drop anything the model invented, and de-duplicate while keeping order.
    tags = []
    for tag in raw_tags:
        if isinstance(tag, str) and tag in ALLOWED_TAGS and tag not in tags:
            tags.append(tag)

    # Top up from the allowlist rather than failing the request over a model
    # that returned four usable tags instead of six.
    if len(tags) < MIN_TAGS:
        for tag in sorted(ALLOWED_TAGS):
            if len(tags) >= MIN_TAGS:
                break
            if tag not in tags:
                tags.append(tag)

    return {"analysis": analysis.strip(), "suggested_tags": tags[:MAX_TAGS]}


def rewrite(content: str, tag: str, section_label: str) -> str:
    instruction = TAG_INSTRUCTIONS.get(tag)
    if not instruction:
        raise errors.invalid_tag(tag)

    raw = _generate(
        _REWRITE_PROMPT.format(
            instruction=instruction, section_label=section_label, content=content
        )
    )

    result = re.sub(r"^```(?:\w+)?\s*", "", raw.strip())
    result = re.sub(r"```$", "", result).strip().strip('"').strip()
    if not result:
        raise errors.invalid_llm_response()
    return result
