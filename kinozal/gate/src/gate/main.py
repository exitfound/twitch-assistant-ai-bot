"""The gate's entry point: the FastAPI app (uvicorn gate.main:app) and hall_loop() beside it for its whole life."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gate import bridge
from gate.routers import api, entry, hall, login


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(bridge.hall_loop())
    yield
    task.cancel()


# Every handler is async, so the gate's state changes on the event loop only (gate/state.py)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
for module in (entry, login, hall, api):
    app.include_router(module.router)
