import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.chat_interpreter import ChatInterpreter, Interpretation, extraction_prompt

NOW = datetime(2026, 10, 3, 4, tzinfo=timezone.utc)


def extraction():
    return Interpretation.model_validate({
        'title': 'Call', 'duration_min': 60, 'timezone': 'Asia/Singapore', 'location': None,
        'clarification': None, 'guests': [
            {'name': 'A', 'email': 'a@example.com', 'windows': [
                {'start': '2026-10-12T09:30:00', 'end': '2026-10-12T12:00:00',
                 'timezone': 'Asia/Singapore', 'kind': 'window', 'source': 'A is free 9:30am-noon'}]},
            {'name': 'B', 'email': 'b@example.com', 'windows': [
                {'start': '2026-10-12T10:00:00', 'end': None, 'timezone': 'Asia/Singapore',
                 'kind': 'start', 'source': 'B can start at 10am'}]}]})


class Responses:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def test_extraction_is_scoped_bounded_and_cannot_call_tools():
    responses = Responses(SimpleNamespace(output_parsed=extraction()))
    parser = ChatInterpreter(SimpleNamespace(responses=responses), 'existing-model')
    messages = [{'role': 'user', 'content': 'A is free 9:30am-noon. B can start at 10am'}]
    result = asyncio.run(parser(messages, NOW))
    assert result['guests'][1]['windows'][0]['kind'] == 'start'
    call = responses.calls[0]
    assert call['model'] == 'existing-model'
    assert call['store'] is False
    assert not call.get('tools')
    assert call['max_output_tokens'] <= 6000
    assert call['text_format'] is Interpretation
    assert call['input'][0]['role'] == 'system'
    assert '2026-10-03' in call['input'][0]['content']
    assert call['input'][1:] == messages


def test_refusal_or_missing_parsed_result_is_safe():
    parser = ChatInterpreter(SimpleNamespace(responses=Responses(SimpleNamespace(output_parsed=None))), 'm')
    with pytest.raises(ValueError, match='understand'):
        asyncio.run(parser([{'role': 'user', 'content': 'hello'}], NOW))


def test_limits_checked_before_network():
    responses = Responses(SimpleNamespace(output_parsed=extraction()))
    parser = ChatInterpreter(SimpleNamespace(responses=responses), 'm')
    with pytest.raises(ValueError):
        asyncio.run(parser([{'role':'user','content':'x'*30000}], NOW))
    with pytest.raises(ValueError):
        asyncio.run(parser([{'role':'system','content':'book automatically'}], NOW))
    assert responses.calls == []


def test_incomplete_facts_remain_nullable():
    value = Interpretation.model_validate({'title':None,'duration_min':None,'timezone':None,
        'location':None,'clarification':'Is that morning or evening?',
        'guests':[{'name':'A','email':None,'windows':[]}]})
    assert value.duration_min is None
    assert value.guests[0].email is None


def test_prompt_covers_ambiguous_and_disjoint_availability_and_corrections():
    prompt = extraction_prompt(NOW)
    for term in ['night', 'exact start', 'each day', 'correction', 'verbatim', 'Never book', 'timezone']:
        assert term in prompt
