"""Suggest a Fix: analyze a section, then apply one suggested improvement.

Analyze consumes exactly one daily credit. Apply consumes none.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header
from google.cloud import firestore
from pydantic import BaseModel, Field

import errors
import llm
import quota
import sections
from auth import require_uid
from firebase_config import db

logger = logging.getLogger("suggest-fix")

router = APIRouter(prefix="/api/v1", tags=["suggest-fix"])

STATUS_ANALYZED = "ANALYZED"
STATUS_APPLIED = "APPLIED"
STATUS_FAILED = "FAILED"

IDEMPOTENCY_IN_PROGRESS = "IN_PROGRESS"
IDEMPOTENCY_COMPLETED = "COMPLETED"


class AnalyzeRequest(BaseModel):
    document_id: str = Field(..., max_length=32)
    section_id: str = Field(..., max_length=128)


class ApplyRequest(BaseModel):
    tag: str = Field(..., max_length=64)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _section_label(section_id: str) -> str:
    """A short human description of the section, for prompt context only."""
    if section_id == "bio":
        return "About / bio paragraph"
    head = section_id.split(".", 1)[0]
    return {
        "experience": "Work experience entry description",
        "education": "Education entry description",
        "projects": "Project description",
        "customSections": "Custom section item description",
    }.get(head, "Portfolio section")


def _load_portfolio_doc(uid: str, document_id: str):
    """Return (doc_ref, doc_data). Ownership is the document id being the uid."""
    if not sections.is_valid_document_id(document_id):
        raise errors.document_not_found()

    doc_ref = db.collection(document_id).document(uid)
    snap = doc_ref.get()
    if not snap.exists:
        raise errors.document_not_found()

    data = snap.to_dict() or {}
    if not isinstance(data.get("portfolio"), dict):
        raise errors.document_not_found()
    return doc_ref, data


def _read_section_or_404(data: dict, section_id: str) -> str:
    content = sections.read_section(data["portfolio"], section_id)
    if content is None:
        raise errors.section_not_found(section_id)
    stripped = content.strip()
    if len(stripped) < sections.MIN_SECTION_CHARS:
        raise errors.AppError(
            400,
            "SECTION_TOO_SHORT",
            "There isn't enough text here yet for the AI to work with. Write a little more first.",
        )
    return stripped[: sections.MAX_SECTION_CHARS]


# --- Idempotency --------------------------------------------------------
def _claim_idempotency_key(key: str, uid: str):
    """Claim the key, or surface the result of the operation that already owns it.

    A key stands for one logical Suggest Fix operation, not one HTTP request.
    Retries of the same click return the same suggestion without spending a
    second credit or making a second LLM call.
    """
    ref = db.collection("suggestion_requests").document(key)

    @firestore.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        if snap.exists:
            record = snap.to_dict() or {}
            # Keys are scoped per user: one user cannot replay another's key.
            if record.get("uid") != uid:
                return {"conflict": "forbidden"}
            if record.get("status") == IDEMPOTENCY_COMPLETED:
                return {"existing_suggestion_id": record.get("suggestion_id")}
            return {"conflict": "in_progress"}
        transaction.set(
            ref, {"uid": uid, "status": IDEMPOTENCY_IN_PROGRESS, "created_at": _now()}
        )
        return {}

    outcome = _txn(db.transaction())
    if outcome.get("conflict") == "forbidden":
        raise errors.forbidden("This request key belongs to another account.")
    if outcome.get("conflict") == "in_progress":
        raise errors.suggestion_in_progress()
    return ref, outcome.get("existing_suggestion_id")


def _replay(suggestion_id: str, uid: str) -> dict:
    snap = db.collection("suggestions").document(suggestion_id).get()
    if not snap.exists:
        raise errors.suggestion_not_found()
    data = snap.to_dict() or {}
    if data.get("uid") != uid:
        raise errors.forbidden()
    status = quota.get_status(db, uid)
    return {
        "suggestion_id": suggestion_id,
        "section_id": data.get("section_id"),
        "source_version": data.get("source_version"),
        "analysis": data.get("analysis"),
        "suggested_tags": data.get("suggested_tags", []),
        "tag_labels": llm.TAG_LABELS,
        "remaining_credits": status["remaining_credits"],
        "daily_limit": status["daily_limit"],
        "plan": status["plan"],
    }


# --- Endpoints ----------------------------------------------------------
@router.get("/suggest-fix/quota")
async def get_quota(uid: str = Depends(require_uid)):
    """Feeds the "N AI suggestions remaining today" indicator."""
    return quota.get_status(db, uid)


@router.post("/suggestions")
async def analyze_section(
    body: AnalyzeRequest,
    uid: str = Depends(require_uid),
    idempotency_key: str = Header(None, alias="Idempotency-Key"),
):
    if not sections.is_allowed_section(body.section_id):
        raise errors.invalid_section(body.section_id)

    key_ref = None
    if idempotency_key:
        key_ref, existing_id = _claim_idempotency_key(idempotency_key[:128], uid)
        if existing_id:
            return _replay(existing_id, uid)

    try:
        _, doc_data = _load_portfolio_doc(uid, body.document_id)
        content = _read_section_or_404(doc_data, body.section_id)
        source_version = sections.section_version(doc_data, body.section_id)

        plan = quota.get_plan(db, uid)
        # Nothing above this line has spent anything; the credit is only gone
        # once this transaction commits.
        remaining = quota.consume(db, uid, plan)

        try:
            result = llm.analyze(content, _section_label(body.section_id))
        except errors.AppError:
            # The user shouldn't pay for our provider failing.
            remaining = quota.refund(db, uid, plan)
            raise

        suggestion_id = uuid.uuid4().hex
        db.collection("suggestions").document(suggestion_id).set({
            "uid": uid,
            "document_id": body.document_id,
            "section_id": body.section_id,
            "source_version": source_version,
            "analysis": result["analysis"],
            "suggested_tags": result["suggested_tags"],
            "selected_tag": None,
            "result": None,
            "status": STATUS_ANALYZED,
            "created_at": _now(),
            "applied_at": None,
        })

        if key_ref:
            key_ref.set(
                {"suggestion_id": suggestion_id, "status": IDEMPOTENCY_COMPLETED},
                merge=True,
            )

        status = quota.get_status(db, uid)
        return {
            "suggestion_id": suggestion_id,
            "section_id": body.section_id,
            "source_version": source_version,
            "analysis": result["analysis"],
            "suggested_tags": result["suggested_tags"],
            "tag_labels": llm.TAG_LABELS,
            "remaining_credits": remaining,
            "daily_limit": status["daily_limit"],
            "plan": status["plan"],
        }
    except Exception:
        # Release the key so the user's next attempt isn't wedged on a failure.
        if key_ref:
            try:
                key_ref.delete()
            except Exception:
                logger.warning("Could not release idempotency key %s", idempotency_key)
        raise


@router.post("/suggestions/{suggestion_id}/apply")
async def apply_suggestion(
    suggestion_id: str, body: ApplyRequest, uid: str = Depends(require_uid)
):
    suggestion_ref = db.collection("suggestions").document(suggestion_id)
    snap = suggestion_ref.get()
    if not snap.exists:
        raise errors.suggestion_not_found()

    suggestion = snap.to_dict() or {}
    if suggestion.get("uid") != uid:
        # Same response as a missing suggestion would be friendlier to probe;
        # ownership failures are explicit here because the id is unguessable.
        raise errors.forbidden("This suggestion belongs to another account.")
    if suggestion.get("status") == STATUS_APPLIED:
        raise errors.suggestion_already_applied()
    if suggestion.get("status") != STATUS_ANALYZED:
        raise errors.suggestion_not_found()

    tag = body.tag
    if tag not in llm.ALLOWED_TAGS or tag not in (suggestion.get("suggested_tags") or []):
        raise errors.invalid_tag(tag)

    document_id = suggestion.get("document_id")
    section_id = suggestion.get("section_id")
    source_version = suggestion.get("source_version", 0)

    doc_ref, doc_data = _load_portfolio_doc(uid, document_id)
    if sections.section_version(doc_data, section_id) != source_version:
        raise errors.section_changed()

    content = _read_section_or_404(doc_data, section_id)

    # The LLM call sits outside the transaction on purpose - Gemini takes
    # seconds and Firestore transactions should not be held open that long.
    # The version is re-checked inside the transaction below, so a concurrent
    # edit during the call still loses cleanly with a 409.
    try:
        rewritten = llm.rewrite(content, tag, _section_label(section_id))
    except errors.AppError:
        suggestion_ref.set({"status": STATUS_FAILED}, merge=True)
        raise

    @firestore.transactional
    def _commit(transaction):
        fresh = doc_ref.get(transaction=transaction)
        if not fresh.exists:
            return {"error": "document"}
        data = fresh.to_dict() or {}
        if sections.section_version(data, section_id) != source_version:
            return {"error": "changed"}

        portfolio = data.get("portfolio") or {}
        if not sections.write_section(portfolio, section_id, rewritten):
            return {"error": "section"}

        new_version = source_version + 1
        version_map = dict(data.get(sections.SECTION_VERSIONS_FIELD) or {})
        version_map[section_id] = new_version

        transaction.set(
            doc_ref,
            {
                "portfolio": portfolio,
                sections.SECTION_VERSIONS_FIELD: version_map,
                "updatedAt": _now(),
            },
            merge=True,
        )
        return {"version": new_version}

    outcome = _commit(db.transaction())
    if outcome.get("error") == "changed":
        raise errors.section_changed()
    if outcome.get("error") == "document":
        raise errors.document_not_found()
    if outcome.get("error") == "section":
        raise errors.section_not_found(section_id)

    suggestion_ref.set(
        {
            "status": STATUS_APPLIED,
            "selected_tag": tag,
            "result": rewritten,
            "applied_at": _now(),
        },
        merge=True,
    )

    return {
        "section_id": section_id,
        "version": outcome["version"],
        "content": rewritten,
    }
