# Build context: repository root.   docker build -f services/gateway/Dockerfile -t mcp-platform/gateway:1.0.0 .
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app && mkdir /data && chown 10001 /data
WORKDIR /app
COPY services/gateway/requirements.txt .
RUN pip install -r requirements.txt
COPY services/gateway/gateway ./gateway
USER 10001
EXPOSE 8080
# ONE process, many threads: upstream connection state and the health loop live in process memory.
# (Scale-out needs Postgres + shared state; see docs/SYSTEM_DESIGN.md.)
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "16", "--timeout", "120", \
     "--worker-tmp-dir", "/dev/shm", "--access-logfile", "-", "gateway.wsgi:app"]
