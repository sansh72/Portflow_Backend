"""Error codes and the exception type the Suggest Fix endpoints raise.

Every failure the frontend needs to branch on gets a stable machine-readable
`code`. Provider errors (Gemini, Firestore) are never surfaced raw.
"""

from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str, **extra):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra
        super().__init__(message)


# --- Auth / ownership ---------------------------------------------------
def unauthenticated(message="Sign in to continue."):
    return AppError(401, "UNAUTHENTICATED", message)


def forbidden(message="You don't have access to this resource."):
    return AppError(403, "FORBIDDEN", message)


# --- Lookup -------------------------------------------------------------
def document_not_found():
    return AppError(404, "DOCUMENT_NOT_FOUND", "Portfolio not found.")


def section_not_found(section_id: str):
    return AppError(404, "SECTION_NOT_FOUND", f"Section '{section_id}' not found.")


def suggestion_not_found():
    return AppError(404, "SUGGESTION_NOT_FOUND", "Suggestion not found.")


# --- Business rules -----------------------------------------------------
def quota_reached(message: str):
    return AppError(429, "SUGGEST_FIX_QUOTA_REACHED", message, remaining_credits=0)


def invalid_tag(tag: str):
    return AppError(400, "INVALID_TAG", f"'{tag}' is not a valid improvement tag.")


def invalid_section(section_id: str):
    return AppError(400, "INVALID_SECTION", f"'{section_id}' is not an editable section.")


def suggestion_already_applied():
    return AppError(
        409,
        "SUGGESTION_ALREADY_APPLIED",
        "This suggestion has already been applied.",
    )


def section_changed():
    return AppError(
        409,
        "SECTION_CHANGED",
        "This section changed after the suggestion was generated. Generate a new suggestion.",
    )


def suggestion_in_progress():
    return AppError(
        409,
        "SUGGESTION_IN_PROGRESS",
        "This request is still being processed. Try again in a moment.",
    )


# --- LLM ----------------------------------------------------------------
def llm_error():
    return AppError(502, "LLM_ERROR", "The AI service is unavailable right now. Please try again.")


def invalid_llm_response():
    return AppError(502, "INVALID_LLM_RESPONSE", "The AI returned an unexpected response. Please try again.")


async def app_error_handler(request: Request, exc: AppError):
    body = {"code": exc.code, "message": exc.message}
    body.update(exc.extra)
    return JSONResponse(status_code=exc.status_code, content=body)
