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

# A wide pool on purpose. With only seven options and a six-tag minimum, every
# section got nearly the same list back - the choice carried no information.
ALLOWED_TAGS = {
    "make_concise",
    "more_professional",
    "highlight_achievements",
    "more_technical",
    "more_engaging",
    "remove_repetition",
    "rewrite_completely",
    "add_specifics",
    "quantify_impact",
    "cut_buzzwords",
    "active_voice",
    "stronger_opening",
    "simplify_language",
    "vary_sentences",
    "tighten_focus",
    "show_ownership",
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

    # Every instruction below inherits the global rule in the rewrite prompt:
    # never invent facts. These are phrased to surface what is already there
    # rather than to fill gaps.
    "add_specifics":
        "Replace vague phrasing with the specific technologies, tools and scope already "
        "implied by the text. Do not invent details that are not present.",

    "quantify_impact":
        "Foreground any numbers, scale or outcomes already present in the text. "
        "Never invent figures - if none exist, sharpen the described outcome instead.",

    "cut_buzzwords":
        "Remove filler and buzzwords such as 'passionate', 'cutting-edge', 'leveraging' "
        "and 'robust', keeping the substance underneath.",

    "active_voice":
        "Rewrite passive constructions in the active voice so it is clear who did what.",

    "stronger_opening":
        "Rewrite so the first sentence leads with the most compelling point instead of "
        "building up to it.",

    "simplify_language":
        "Say the same thing in plainer language with shorter sentences, without dumbing "
        "down the technical content.",

    "vary_sentences":
        "Vary sentence length and structure so the writing stops sounding uniform.",

    "tighten_focus":
        "Cut whatever strays from the section's main point so it makes one argument well.",

    "show_ownership":
        "Make clear what this person personally did, rather than what the team or company did.",
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
    "add_specifics": "Add specifics",
    "quantify_impact": "Quantify impact",
    "cut_buzzwords": "Cut buzzwords",
    "active_voice": "Active voice",
    "stronger_opening": "Stronger opening",
    "simplify_language": "Simpler language",
    "vary_sentences": "Vary sentences",
    "tighten_focus": "Tighten focus",
    "show_ownership": "Show ownership",
}

# Few enough that picking them is a real judgement about this section.
MIN_TAGS = 3
MAX_TAGS = 6

# What a collection review can say about one entry. Advisory only - nothing is
# deleted or rewritten on the strength of these; the user decides.
VERDICTS = ("keep", "rewrite", "remove")

VERDICT_LABELS = {
    "keep": "Keep as is",
    "rewrite": "Worth rewriting",
    "remove": "Consider removing",
}

_ANALYSIS_PROMPT = """You are reviewing one section of a personal portfolio.

Explain what could be improved about it. Do NOT rewrite the section, do not
produce an improved version, and do not quote a replacement. Two or three
sentences, addressed to the author, plain text only.

Then choose between {min_tags} and {max_tags} improvement tags from exactly this
list, picking ONLY the ones that genuinely apply to this specific section and
ordering them most useful first. Do not pad the list to reach the maximum -
three well-chosen tags beat six generic ones. Your analysis above and the tags
you pick should agree with each other.

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

    # Only pad when the model gave us almost nothing to show. Topping every
    # response up to the minimum was what made each section come back with the
    # same alphabetical list regardless of what it said. Order is the model's,
    # since it was asked to rank by usefulness.
    if not tags:
        tags = ["make_concise", "more_professional", "highlight_achievements"]

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


_REVIEW_PROMPT = """You are reviewing the {label} section of a personal portfolio
as a whole, the way a hiring manager skimming it would.

For each entry, decide one verdict:
- "keep"    - strong and worth keeping as is
- "rewrite" - worth keeping but the writing undersells it
- "remove"  - weak, redundant, or dilutes the stronger entries

The page lays these out {layout}, so about {keep_target} is the number that
displays well. If there are more than that, say plainly which ones earn their
place and which are the weakest - the user is asking you to help them cut.

Be willing to say "remove" when an entry adds nothing next to the others; a
shorter, stronger list beats a long flat one. Equally, do not invent problems -
if everything is genuinely strong, say so and keep them all.

Give a short overall verdict too: is this a strong set, are there too many, are
any two covering the same ground?

Return only valid JSON, no markdown fences, in this shape:
{{"analysis": "...", "items": [{{"index": 0, "verdict": "keep", "reason": "one short sentence"}}]}}

Include exactly one item per entry, using the index shown.

Entries:
{entries}
"""


def review_collection(
    entries: list, label: str, keep_target: int = 0, layout: str = ""
) -> dict:
    """Advise on a whole collection. Returns {"analysis", "items"}, validated.

    `keep_target` and `layout` describe how the page actually renders the
    collection, so the advice can be specific about how many to keep rather
    than vaguely suggesting the list is long.
    """
    rendered = "\n\n".join(
        f"[{e['index']}] {e['title'] or '(untitled)'}\n{e['description'] or '(no description)'}"
        for e in entries
    )
    raw = _generate(_REVIEW_PROMPT.format(
        label=label,
        entries=rendered,
        keep_target=keep_target or len(entries),
        layout=layout or "in a grid",
    ))

    try:
        parsed = json.loads(_strip_fences(raw))
    except (json.JSONDecodeError, ValueError):
        logger.error("Gemini review was not JSON: %s", raw[:200])
        raise errors.invalid_llm_response()

    if not isinstance(parsed, dict):
        raise errors.invalid_llm_response()

    analysis = parsed.get("analysis")
    if not isinstance(analysis, str) or not analysis.strip():
        raise errors.invalid_llm_response()

    valid_indexes = {e["index"] for e in entries}
    by_index = {}
    for item in parsed.get("items") or []:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        verdict = item.get("verdict")
        # Never trust an index or verdict the model made up.
        if index not in valid_indexes or verdict not in VERDICTS or index in by_index:
            continue
        reason = item.get("reason")
        by_index[index] = {
            "index": index,
            "verdict": verdict,
            "reason": reason.strip() if isinstance(reason, str) else "",
        }

    # An entry the model skipped defaults to "keep" - silence is not a reason
    # to suggest deleting someone's work.
    items = []
    for entry in entries:
        item = by_index.get(entry["index"]) or {
            "index": entry["index"], "verdict": "keep", "reason": "",
        }
        items.append({**item, "title": entry["title"]})

    return {"analysis": analysis.strip(), "items": items}
