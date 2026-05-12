from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from web_tool.services.tuning_bridge import run_virtual_round

router = APIRouter(tags=["llm_debug"])

_store: dict[str, str] = {
    "last_prompt": "[stub] 尚未接入真实流水线。",
    "last_response": "[stub] 无响应。",
}


class LlmDebugBody(BaseModel):
    prompt: str | None = None
    response: str | None = None


class PidBody(BaseModel):
    p: float | None = None
    i: float | None = None
    d: float | None = None


class VirtualRoundBody(BaseModel):
    round_index: int = 1
    samples: list[dict] = Field(default_factory=list)
    history_text: str | None = None
    current_pid: PidBody | None = None


@router.get("/debug/llm")
def llm_debug_get() -> dict[str, str]:
    return {"prompt": _store["last_prompt"], "response": _store["last_response"]}


@router.post("/debug/llm")
def llm_debug_set(body: LlmDebugBody) -> dict[str, str]:
    if body.prompt is not None:
        _store["last_prompt"] = body.prompt
    if body.response is not None:
        _store["last_response"] = body.response
    return {"prompt": _store["last_prompt"], "response": _store["last_response"]}


@router.post("/debug/virtual-round")
async def llm_debug_virtual_round(body: VirtualRoundBody) -> dict:
    payload = {
        "round_index": body.round_index,
        "samples": body.samples,
        "history_text": body.history_text,
        "current_pid": body.current_pid.model_dump() if body.current_pid else None,
    }
    result = await run_in_threadpool(run_virtual_round, payload)
    if result.get("prompt_text"):
        _store["last_prompt"] = str(result["prompt_text"])
    if result.get("response_text"):
        _store["last_response"] = str(result["response_text"])
    return result
