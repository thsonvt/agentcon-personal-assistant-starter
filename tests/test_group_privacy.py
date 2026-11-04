import importlib
import logging
import pytest


def test_guest_link_capability_is_redacted_from_access_log():
    try: module=importlib.import_module('app.group_privacy')
    except ModuleNotFoundError: pytest.fail('Private link log redaction is missing')
    record=logging.LogRecord('uvicorn.access',logging.INFO,'',0,'%s - "%s %s HTTP/%s" %d',
                             ('client','GET','/m/secret-participant-token?timezone=Asia%2FRiyadh','1.1',200),None)
    assert module.PrivateMeetingLogFilter().filter(record)
    assert 'secret-participant-token' not in record.getMessage()
    assert '/m/[private]' in record.getMessage()


def test_nonprivate_access_logs_remain_unchanged():
    try: module=importlib.import_module('app.group_privacy')
    except ModuleNotFoundError: pytest.fail('Private link log redaction is missing')
    record=logging.LogRecord('uvicorn.access',logging.INFO,'',0,'%s - "%s %s HTTP/%s" %d',
                             ('client','GET','/health','1.1',200),None)
    before=record.getMessage();assert module.PrivateMeetingLogFilter().filter(record)
    assert record.getMessage()==before
