FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    APP_DB=/data/app.db

WORKDIR /app
COPY app ./app
COPY main.py ./

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 10001 appuser && mkdir -p /data && chown appuser /data
USER appuser

EXPOSE 8080
CMD ["python", "main.py"]
