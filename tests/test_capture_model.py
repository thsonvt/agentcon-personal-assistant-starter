import asyncio
import json
from types import SimpleNamespace

import pytest

from app.capture_model import CaptureModel


class Responses:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        value = kwargs['text_format'].model_validate(self.result) if self.result is not None else None
        return SimpleNamespace(output_parsed=value)


def model(result):
    responses = Responses(result)
    return CaptureModel(SimpleNamespace(responses=responses), 'capture-test'), responses


def source(item_id=1, **kwargs):
    return dict(id=item_id, user_id=1, session_id='one', status='ready', included=True,
                kind='text', original_text='A useful observation', extracted_text='', category='observation', **kwargs)


def summary(ids=None):
    return {'takeaways': [{'text': 'A useful takeaway', 'source_ids': ids or [1]}],
            'observations': [], 'questions': []}


def test_summary_uses_structured_source_data_and_no_tools():
    parser, responses = model(summary())
    result = asyncio.run(parser.summarize('Talk', [source()]))
    assert result == summary()
    call = responses.calls[0]
    assert call['store'] is False and not call.get('tools')
    assert call['max_output_tokens'] <= 6000
    assert call['model'] == 'capture-test'
    assert call['input'][0]['role'] == 'system'
    assert json.loads(call['input'][1]['content'])['items'][0]['id'] == 1
    assert 'untrusted' in call['input'][0]['content']


@pytest.mark.parametrize('change', [{'included': False}, {'status': 'failed'}, {'status': 'pending'}])
def test_summary_excludes_unusable_sources_and_rejects_citing_them(change):
    bad = source(2)
    bad.update(change)
    parser, responses = model(summary([2]))
    with pytest.raises(ValueError):
        asyncio.run(parser.summarize('Talk', [source(), bad]))
    assert [s['id'] for s in json.loads(responses.calls[0]['input'][1]['content'])['items']] == [1]


def test_unknown_citations_and_cross_session_inputs_are_rejected():
    parser, _ = model(summary([99]))
    with pytest.raises(ValueError):
        asyncio.run(parser.summarize('Talk', [source()]))
    other = source(2)
    other['session_id'] = 'two'
    with pytest.raises(ValueError):
        asyncio.run(parser.summarize('Talk', [source(), other]))


@pytest.mark.parametrize('result', [None, {'takeaways': [], 'observations': [], 'questions': []},
    {'takeaways': [{'text': ' ', 'source_ids': [1]}], 'observations': [], 'questions': []}])
def test_empty_or_refused_summaries_fail(result):
    parser, _ = model(result)
    with pytest.raises(ValueError):
        asyncio.run(parser.summarize('Talk', [source()]))


def test_image_uses_data_uri_and_preserves_uncertainty():
    parser, responses = model({'text': 'Readable heading', 'uncertainty': 'Chart values are unreadable.'})
    assert asyncio.run(parser.extract_image(b'image', 'image/png')) == 'Readable heading\n\nUncertainty: Chart values are unreadable.'
    call = responses.calls[0]
    assert call['input'][1]['content'][0]['image_url'] == 'data:image/png;base64,aW1hZ2U='
    assert call['store'] is False and not call.get('tools')
    assert 'illegible' in call['input'][0]['content']


def test_unreadable_image_is_preserved_as_uncertainty():
    parser, _ = model({'text': '', 'uncertainty': 'The slide is unreadable.'})
    assert 'unreadable' in asyncio.run(parser.extract_image(b'image', 'image/jpeg'))


@pytest.mark.parametrize('kind', ['observation', 'question', 'review'])
def test_classification_only_returns_bounded_category(kind):
    parser, responses = model({'category': kind})
    assert asyncio.run(parser.classify('What should I explore?', 'Talk')) == kind
    prompt = responses.calls[0]['input'][0]['content']
    assert 'unrelated' in prompt and 'review' in prompt
    assert not responses.calls[0].get('tools')


def test_input_limits_fail_before_provider_call():
    parser, responses = model(None)
    for call in [parser.classify('x' * 10001, 'Talk'), parser.classify('', 'Talk'),
                 parser.extract_image(b'', 'image/png'), parser.extract_image(b'gif', 'image/gif'),
                 parser.extract_image(b'x' * (5 * 1024 * 1024 + 1), 'image/jpeg'),
                 parser.summarize('Talk', [])]:
        with pytest.raises(ValueError):
            asyncio.run(call)
    assert responses.calls == []


@pytest.mark.parametrize('operation,result', [('image', {'text': ' ', 'uncertainty': ''}),
    ('image', None), ('classify', None), ('classify', {'category': 'book_calendar'})])
def test_invalid_extraction_and_classification_are_rejected(operation, result):
    parser, _ = model(result)
    with pytest.raises(ValueError):
        asyncio.run(parser.extract_image(b'pic', 'image/png') if operation == 'image' else parser.classify('a note', 'Talk'))


def test_summary_rejects_duplicate_ids_and_oversized_input_before_call():
    parser, responses = model(summary())
    oversized = [source(i) for i in range(1, 22)]
    for item in oversized:
        item['original_text'] = 'x' * 10000
    for items in [[source(), source()], oversized]:
        with pytest.raises(ValueError):
            asyncio.run(parser.summarize('Talk', items))
    assert responses.calls == []


def test_provider_timeout_is_finite(monkeypatch):
    import app.capture_model as module
    original = asyncio.wait_for
    timeouts = []

    async def limited(awaitable, timeout):
        timeouts.append(timeout)
        return await original(awaitable, timeout=0.001)

    class SlowResponses:
        async def parse(self, **kwargs):
            await asyncio.Event().wait()

    monkeypatch.setattr(module.asyncio, 'wait_for', limited)
    parser = CaptureModel(SimpleNamespace(responses=SlowResponses()), 'capture-test')
    with pytest.raises(TimeoutError):
        asyncio.run(parser.classify('An observation', 'Talk'))
    assert timeouts == [45]
