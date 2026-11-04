"""Run the real Google Calendar workshop on loopback only.

.venv/bin/python scripts/run_web_local.py
Open http://127.0.0.1:8016/. Connect Google in the browser; AI chat is optional.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from app.workshop import build_workshop_app


if __name__ == '__main__':
    import uvicorn
    load_dotenv(ROOT / '.env')
    # Access logs would expose Google authorization codes in callback URLs.
    uvicorn.run(build_workshop_app(), host='127.0.0.1', port=8016, log_level='warning', access_log=False)
