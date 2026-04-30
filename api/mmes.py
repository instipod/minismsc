from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(tags=["mmes"])


class MmeInfo(BaseModel):
    ip: str
    port: int
    connected_at: datetime


class MmeListResponse(BaseModel):
    count: int
    mmes: list[MmeInfo]


@router.get("/mmes", response_model=MmeListResponse)
async def list_mmes(request: Request) -> MmeListResponse:
    state = request.app.state.app_state
    async with state._lock:
        result = [
            MmeInfo(ip=c.ip, port=c.port, connected_at=c.connected_at)
            for c in state.mmes.values()
        ]
    return MmeListResponse(count=len(result), mmes=result)
