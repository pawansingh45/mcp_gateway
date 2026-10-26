# Build context: repository root. One image, two servers: REF_SERVER=clock|notes
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app && mkdir /data && chown 10001 /data
WORKDIR /app
COPY libs/mcpkit /tmp/mcpkit
COPY services/ref-mcp/requirements.txt .
RUN pip install -r requirements.txt /tmp/mcpkit && rm -rf /tmp/mcpkit
COPY services/ref-mcp/ref_mcp ./ref_mcp
USER 10001
EXPOSE 8080
# notes keeps state in a JSON file, so exactly one worker process.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--worker-tmp-dir", "/dev/shm", "ref_mcp.wsgi:app"]
