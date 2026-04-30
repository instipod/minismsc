from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from config import settings
from db.sms_queue import (
    clear_imsi_mappings as db_clear_imsi_mappings,
    delete_imsi_mapping as db_delete_imsi_mapping,
    upsert_imsi_mapping,
)

router = APIRouter(tags=["imsi-mappings"])


class ImsiMapping(BaseModel):
    imsi: str
    mme_ip: str


class ImsiMappingListResponse(BaseModel):
    count: int
    mappings: list[ImsiMapping]


@router.get("/imsi-mappings", response_model=ImsiMappingListResponse)
async def list_imsi_mappings(request: Request) -> ImsiMappingListResponse:
    state = request.app.state.app_state
    async with state._lock:
        items = [
            ImsiMapping(imsi=imsi, mme_ip=mme_ip)
            for imsi, mme_ip in state.imsi_map.items()
        ]
    return ImsiMappingListResponse(count=len(items), mappings=items)


@router.post("/imsi-mappings", response_model=ImsiMapping, status_code=201)
async def add_imsi_mapping(body: ImsiMapping, request: Request) -> ImsiMapping:
    state = request.app.state.app_state
    await state.update_imsi(body.imsi, body.mme_ip)
    await upsert_imsi_mapping(settings.db_path, body.imsi, body.mme_ip)
    return body


@router.delete("/imsi-mappings", status_code=204)
async def clear_imsi_mappings_endpoint(request: Request) -> None:
    state = request.app.state.app_state
    await state.clear_imsi_map()
    await db_clear_imsi_mappings(settings.db_path)


@router.delete("/imsi-mappings/{imsi}", status_code=204)
async def delete_imsi_mapping_endpoint(imsi: str, request: Request) -> None:
    state = request.app.state.app_state
    deleted = await state.delete_imsi(imsi)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"IMSI {imsi} not found")
    await db_delete_imsi_mapping(settings.db_path, imsi)
