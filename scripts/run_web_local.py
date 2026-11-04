"""Run Pippa's REAL web backend for the configured owner on loopback only.

.venv/bin/python scripts/run_web_local.py
Open http://127.0.0.1:8016/app. Uses .env; calendar changes are real.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from app.config import load_settings
from app.local_web import build_local_web
from app.wiring import build_runtime


if __name__ == '__main__':
    import uvicorn
    load_dotenv(ROOT / '.env')
    runtime = build_runtime(load_settings())
    # Background WhatsApp jobs and webhook routes are not started by local web.
    uvicorn.run(build_local_web(runtime), host='127.0.0.1', port=8016, log_level='warning')
