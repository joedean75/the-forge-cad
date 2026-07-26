"""LLM configuration endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.security import current_account
from app.services import accounts, llm_client

router = APIRouter(prefix="/api/llm", tags=["llm"])


class LlmModelUpdate(BaseModel):
    model: str


@router.get("/config")
def get_llm_config(acc: accounts.Account = Depends(current_account)):
    return llm_client.current_config()


@router.post("/model")
def set_llm_model(body: LlmModelUpdate, acc: accounts.Account = Depends(current_account)):
    """Switch the active chat-completion model.

    This is process-global state shared by every tenant, so it is restricted to
    authenticated callers and to models the configured provider actually offers —
    an arbitrary string here would break text-to-CAD and Copilot for everyone
    until the next restart.
    """
    allowed = llm_client.available_models()
    if body.model not in allowed:
        raise HTTPException(status_code=422, detail="unknown model for the configured provider")
    llm_client.set_model(body.model)
    return llm_client.current_config()
