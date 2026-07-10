"""LLM configuration endpoints."""

from fastapi import APIRouter
from pydantic import BaseModel

from app.services import llm_client

router = APIRouter(prefix="/api/llm", tags=["llm"])


class LlmModelUpdate(BaseModel):
    model: str


@router.get("/config")
def get_llm_config():
    return llm_client.current_config()


@router.post("/model")
def set_llm_model(body: LlmModelUpdate):
    llm_client.set_model(body.model)
    return llm_client.current_config()
