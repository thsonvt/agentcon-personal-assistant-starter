FROM python:3.12-slim

WORKDIR /srv
COPY pyproject.toml LICENSE THIRD_PARTY_NOTICES.md ./
COPY app ./app
RUN pip install --no-cache-dir .
COPY main.py ./
COPY scripts/event_networking_admin.py ./scripts/event_networking_admin.py

ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "uvicorn main:app --no-proxy-headers --host 0.0.0.0 --port ${PORT:-8080}"]
