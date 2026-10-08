FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY ui ./ui
COPY data/public ./data/public
COPY data/research/channel_checks_v2.json ./data/research/channel_checks_v2.json
RUN pip install --no-cache-dir -e . && mkdir -p /app/runtime

EXPOSE 8000 8501
