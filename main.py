"""Server entry point: uvicorn main:app"""

import logging

from dotenv import load_dotenv

from app.config import load_settings
from app.webhook import create_app
from app.wiring import build_runtime, sgt_now

logging.basicConfig(level=logging.INFO)
load_dotenv()
settings = load_settings()
runtime = build_runtime(settings)
app = create_app(
    verify_token=settings.wa_verify_token,
    app_secret=settings.wa_app_secret,
    store=runtime.store,
    handle=runtime.router.handle,
    now=sgt_now,
    background_jobs=runtime.jobs,
    routes=runtime.routes,
    status_handler=runtime.status_handler,
    capture_ingest=runtime.capture_ingest,
)
