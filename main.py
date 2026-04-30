import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.imsi_mappings import router as imsi_router
from api.mmes import router as mmes_router
from api.sms import router as sms_router
from config import settings
from db.sms_queue import init_db, load_imsi_mappings
from sctp_server import start_sctp_server
from sms.retry import start_retry_task
from state import AppState

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app_state = AppState()
    app.state.app_state = app_state

    await init_db(settings.db_path)
    log.info("Database initialised at %s", settings.db_path)

    mappings = await load_imsi_mappings(settings.db_path)
    if mappings:
        async with app_state._lock:
            app_state.imsi_map.update(mappings)
        log.info("Loaded %d IMSI mappings from database", len(mappings))

    sctp_server = None
    try:
        loop = asyncio.get_event_loop()
        sctp_server = await start_sctp_server(
            loop, app_state, settings.sgsap_host, settings.sgsap_port
        )
        log.info(
            "SGsAP SCTP server listening on %s:%d",
            settings.sgsap_host,
            settings.sgsap_port,
        )
    except OSError as exc:
        log.warning(
            "SCTP server not started (%s) — SGsAP connections unavailable. "
            "REST API is still functional.",
            exc,
        )

    retry_task = asyncio.create_task(
        start_retry_task(app_state, settings.db_path, settings.sms_retry_interval)
    )

    yield

    retry_task.cancel()
    try:
        await retry_task
    except asyncio.CancelledError:
        pass

    if sctp_server is not None:
        sctp_server.close()
        await sctp_server.wait_closed()
        log.info("SGsAP SCTP server stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Mini MSC",
        version="0.1.0",
        description="SGsAP MSC server with REST management API",
        lifespan=lifespan,
    )
    app.include_router(mmes_router)
    app.include_router(imsi_router)
    app.include_router(sms_router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
