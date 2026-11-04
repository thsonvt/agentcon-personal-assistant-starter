"""Single-attempt transactional host email; acceptance does not mean delivery."""

from contextlib import nullcontext

import httpx


class LoopsBookingEmail:
    def __init__(self, api_key, transactional_id, client=None):
        self._api_key = api_key
        self.transactional_id = transactional_id
        self._client = client

    @property
    def ready(self):
        return bool(self._api_key and self.transactional_id)

    def send(self, payload, request_id):
        if not self.ready:
            return "unconfigured"
        try:
            context = nullcontext(self._client) if self._client is not None else httpx.Client()
            with context as client:
                response = client.post(
                    "https://app.loops.so/api/v1/transactional",
                    headers={
                        "Authorization": "Bearer " + self._api_key,
                        "Idempotency-Key": "pippa-host-" + request_id,
                    },
                    json={
                        "email": payload["email"],
                        "dataVariables": payload["dataVariables"],
                        "transactionalId": payload.get('transactionalId') or self.transactional_id,
                        "addToAudience": False,
                    },
                    timeout=10,
                    follow_redirects=False,
                )
            if 400 <= response.status_code < 500 and response.status_code != 409:
                return "failed"
            if response.status_code == 200:
                result = response.json()
                if isinstance(result, dict) and result.get("success") is True:
                    return "accepted"
        except (httpx.HTTPError, ValueError, RecursionError):
            # A lost response can follow provider acceptance; never retry here.
            pass
        return "unknown"
