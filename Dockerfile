FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8211 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY docker-entrypoint.py ./

# an unprivileged user owns the process; the entrypoint drops privileges after
# fixing the ownership of the mounted data volume (see docker-entrypoint.py)
RUN useradd --create-home --uid 10001 gateway \
 && mkdir -p /data \
 && chown -R gateway:gateway /app /data

EXPOSE 8211

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8211')+'/healthz',timeout=8)" || exit 1

# --no-access-log: the gateway writes its own jsonl access log
# no --proxy-headers on purpose: X-Forwarded-For is only trusted when TRUST_PROXY=true
ENTRYPOINT ["python", "/app/docker-entrypoint.py"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8211} --no-access-log"]
