FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml requirements.lock README.md ./
COPY src ./src
RUN python -m pip wheel -c requirements.lock --wheel-dir /wheels ".[server]"

FROM python:3.12-slim
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels "modmesh[server]" && rm -rf /wheels && useradd --uid 10001 --create-home modmesh
USER 10001
WORKDIR /home/modmesh
ENV MODMESH_BIND_HOST=0.0.0.0 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8731
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=6 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8731/health', timeout=3).read()"]
ENTRYPOINT ["modmesh-server"]
