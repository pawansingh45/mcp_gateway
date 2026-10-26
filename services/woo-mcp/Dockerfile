# Build context: repository root.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY libs/mcpkit /tmp/mcpkit
COPY services/woo-mcp/requirements.txt .
RUN pip install -r requirements.txt /tmp/mcpkit && rm -rf /tmp/mcpkit
COPY services/woo-mcp/woo_mcp ./woo_mcp
USER 10001
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "60", \
     "--worker-tmp-dir", "/dev/shm", "woo_mcp.wsgi:app"]
