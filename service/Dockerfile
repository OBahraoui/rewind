# Rewind service (webhooks, orchestrator, dashboard, MCP). Deployed by infra/deploy-rewind.sh.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /srv
COPY pyproject.toml README.md LICENSE ./
COPY rewind/ rewind/
COPY service/ service/
RUN pip install --no-cache-dir ".[service]"
RUN useradd --uid 10001 --no-create-home rewind
USER 10001
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn service.main:app --factory --host 0.0.0.0 --port ${PORT} --proxy-headers"]
