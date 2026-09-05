"""Sections as allowlisted paths into a portfolio document.

A portfolio is a single Firestore doc (`sde/{uid}`, `bda/{uid}`, `custom/{uid}`)
shaped `{ portfolio: {...}, logs: [...], isPublished: bool }`. There is no
sections subcollection, so a "section" is a dotted path into the portfolio
object:

    bio
    experience.0.description
    education.1.description
    projects.2.description
    customSections.abc123.items.0.description

Only paths matching one of the patterns below can be read or written, which is
what stops the frontend from pointing the LLM at arbitrary fields.

Per-section versions live in a `sectionVersions` map at the *document* root
(a sibling of `portfolio`), so the PortfolioData shape the renderer consumes
stays untouched. A missing entry means version 0.
"""

import re

# document_id -> Firestore collection. These mirror the template ids the
# frontend already uses in useUserData.
TEMPLATE_COLLECTIONS = {"sde", "bda", "custom"}

SECTION_VERSIONS_FIELD = "sectionVersions"

_INDEX = r"(\d{1,2})"
_CUSTOM_ID = r"([A-Za-z0-9_-]{1,64})"

SECTION_PATTERNS = (
    re.compile(r"^bio$"),
    re.compile(rf"^experience\.{_INDEX}\.description$"),
    re.compile(rf"^education\.{_INDEX}\.description$"),
    re.compile(rf"^projects\.{_INDEX}\.description$"),
    re.compile(rf"^customSections\.{_CUSTOM_ID}\.items\.{_INDEX}\.description$"),
)

# Sections shorter than this aren't worth an LLM call.
MIN_SECTION_CHARS = 30
MAX_SECTION_CHARS = 8000


def is_allowed_section(section_id: str) -> bool:
    if not isinstance(section_id, str) or not section_id:
        return False
    return any(p.match(section_id) for p in SECTION_PATTERNS)


def is_valid_document_id(document_id: str) -> bool:
    return document_id in TEMPLATE_COLLECTIONS


def read_section(portfolio: dict, section_id: str):
    """Return the section's text, or None if the path doesn't resolve.

    Returns None rather than raising for indexes that are in range of the
    pattern but not of the actual data (e.g. `experience.7.description` on a
    portfolio with two jobs).
    """
    parts = section_id.split(".")
    node = portfolio
    for part in parts:
        if isinstance(node, list):
            # customSections is a list keyed by an `id` field, not by index.
            if part.isdigit():
                idx = int(part)
                if idx >= len(node):
                    return None
                node = node[idx]
                continue
            match = next((item for item in node if isinstance(item, dict) and item.get("id") == part), None)
            if match is None:
                return None
            node = match
            continue
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]

    return node if isinstance(node, str) else None


def write_section(portfolio: dict, section_id: str, value: str) -> bool:
    """Set the section's text in place. Returns False if the path is gone.

    Callers run this inside a transaction on a freshly read document, so the
    path is re-validated here rather than trusted from analysis time.
    """
    parts = section_id.split(".")
    node = portfolio
    for part in parts[:-1]:
        if isinstance(node, list):
            if part.isdigit():
                idx = int(part)
                if idx >= len(node):
                    return False
                node = node[idx]
                continue
            match = next((item for item in node if isinstance(item, dict) and item.get("id") == part), None)
            if match is None:
                return False
            node = match
            continue
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]

    leaf = parts[-1]
    if not isinstance(node, dict) or leaf not in node or not isinstance(node[leaf], str):
        return False
    node[leaf] = value
    return True


def section_version(doc_data: dict, section_id: str) -> int:
    versions = (doc_data or {}).get(SECTION_VERSIONS_FIELD) or {}
    try:
        return int(versions.get(section_id, 0))
    except (TypeError, ValueError):
        return 0
