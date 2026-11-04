"""Keep participant-link bearer tokens out of normal HTTP access logs."""
import logging
from urllib.parse import unquote


class PrivateMeetingLogFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            address, method, path, protocol, status = record.args
            if isinstance(path, str) and path.startswith('/m/'):
                record.args = (address, method, '/m/[private]', protocol, status)
            elif isinstance(path, str) and (unquote(path).startswith('/captures/') or unquote(path).startswith('/captures?')):
                record.args = (address, method, '/captures/[private]', protocol, status)
        return True


def install_private_meeting_log_filter():
    logger = logging.getLogger('uvicorn.access')
    if not any(isinstance(f, PrivateMeetingLogFilter) for f in logger.filters):
        logger.addFilter(PrivateMeetingLogFilter())
