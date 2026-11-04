"""Synthetic local notes preview; no provider calls, credentials or production data."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
import uvicorn

from app.capture_pages import CapturePages
from app.capture_store import CaptureStore
from app.store import Store


def make_preview(slide=None):
    now = lambda: datetime.now(timezone.utc)
    database = Store(':memory:')
    captures = CaptureStore(database, now)
    user = SimpleNamespace(id=1, status='active', is_admin=True)
    users = SimpleNamespace(get=lambda ident: user if ident == 1 else None)
    session = captures.start(1, '6500000000', 'Designing agents that finish the job · synthetic demo')
    if slide:
        item = captures.add_item(1, session['id'], 'synthetic-slide', 'image', text='A useful way to evaluate the whole workflow.')
        captures.save_media(1, item['id'], Path(slide).read_bytes(), 'image/png')
        captures.complete_item(1, item['id'], 'Measure task completion, safe recovery, and evidence quality.', 'slide')
    else:
        item = captures.add_item(1, session['id'], 'synthetic-slide-text', 'text', text='Measure task completion, safe recovery, and evidence quality.')
        captures.complete_item(1, item['id'], item['original_text'], 'observation')
    observation = captures.add_item(1, session['id'], 'synthetic-observation', 'text', text='Try replaying a failed conference capture after a restart. Every accepted note should still be there.')
    captures.complete_item(1, observation['id'], observation['original_text'], 'observation')
    question = captures.add_item(1, session['id'], 'synthetic-question', 'text', text='How should we measure citation quality for slide summaries?')
    captures.complete_item(1, question['id'], question['original_text'], 'question')
    review = captures.add_item(1, session['id'], 'synthetic-review', 'text', text='Dinner at seven — unrelated to this talk.')
    captures.complete_item(1, review['id'], review['original_text'], 'review', included=False)
    captures.finish(1, session['id'])
    captures.save_summary(1, session['id'], {
        'takeaways': [{'text': 'Evaluate the complete task: completion, recovery and supporting evidence.', 'source_ids':[item['id']]}],
        'observations': [{'text': 'Test whether accepted notes survive a restart during collection.', 'source_ids':[observation['id']]}],
        'questions': [{'text': 'How should citation quality for slide summaries be measured?', 'source_ids':[question['id']]}],
    })
    pages = CapturePages(captures, users, now, '', 'synthetic-preview-secret-not-for-production')
    app = FastAPI()
    app.include_router(pages.router)
    @app.get('/')
    async def index():
        return RedirectResponse(pages.link(session))
    app.state.database = database  # keep shared in-memory DB alive
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8876)
    parser.add_argument('--slide', help='Optional synthetic PNG slide fixture')
    args = parser.parse_args()
    uvicorn.run(make_preview(args.slide), host='127.0.0.1', port=args.port, access_log=False)
