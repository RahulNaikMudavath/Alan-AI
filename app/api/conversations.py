"""Conversation ingestion routes."""

from fastapi import APIRouter, HTTPException, Response, status

from app.api.deps import ConversationServiceDep
from app.llm.base import LLMError
from app.models import ConversationIngestRequest, ConversationIngestResponse
from app.services.conversation_service import (
    ConversationConflictError,
    ConversationLimitError,
    ExtractionDisabledError,
)
from app.services.extraction import ExtractionOutputError

router = APIRouter(prefix="/conversations", tags=["conversations"])

_CONFLICT = "conversation_id already exists with a different payload"
_DISABLED = "fact extraction is disabled; set RECALLKIT_LLM_PROVIDER"
_BAD_OUTPUT = "extraction provider returned invalid output"
_PROVIDER_FAILED = "extraction provider failed"


@router.post(
    "",
    response_model=ConversationIngestResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": ConversationIngestResponse, "description": "Idempotent replay"},
        409: {"description": _CONFLICT},
        422: {"description": "Request too large or invalid"},
        502: {"description": "Provider failed or returned invalid output"},
        503: {"description": _DISABLED},
    },
)
def ingest_conversation(
    payload: ConversationIngestRequest,
    response: Response,
    service: ConversationServiceDep,
) -> ConversationIngestResponse:
    """Extract memories from a conversation and store them atomically.

    The same ``(user_id, conversation_id)`` with an identical payload replays the
    stored receipt with status 200 and costs nothing. A different payload under
    the same id is a 409 and writes nothing.
    """
    try:
        result, replayed = service.ingest(payload)
    except ConversationConflictError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_CONFLICT) from None
    except ExtractionDisabledError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DISABLED
        ) from None
    except ConversationLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    except ExtractionOutputError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_BAD_OUTPUT) from None
    except LLMError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_PROVIDER_FAILED
        ) from None

    if replayed:
        response.status_code = status.HTTP_200_OK
    return result
