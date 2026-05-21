from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from web_tool.services.tuning_bridge import run_virtual_round

router = APIRouter(tags=["llm_debug"])

_store: dict[str, str] = {"last_prompt": "", "last_response": ""}


class VirtualRoundBody(BaseModel):
    round_index: int = 1
    samples: list[dict] = Field(default_factory=list)
    history_text: str | None = None
    plant_profile: str = "hardware_step"
    prompt_context: dict | None = None
    """落盘子目录：virtual → runtime/virtual_rounds，serial → runtime/serial_rounds"""
    session: str = "virtual"


@router.get("/debug/llm")
def llm_debug_get() -> dict[str, str]:
    return {"prompt": _store["last_prompt"], "response": _store["last_response"]}


@router.post("/debug/virtual-round")
async def llm_debug_virtual_round(body: VirtualRoundBody) -> dict:
    payload = {
        "round_index": body.round_index,
        "samples": body.samples,
        "history_text": body.history_text,
        "plant_profile": body.plant_profile,
        "prompt_context": body.prompt_context,
        "session": body.session,
    }
    try:
        result = await run_in_threadpool(run_virtual_round, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if result.get("prompt_text"):
        _store["last_prompt"] = str(result["prompt_text"])
    if result.get("response_text"):
        _store["last_response"] = str(result["response_text"])
    return result
