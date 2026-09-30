# Образ MCP-сервера правил обмена КД 2.
# Зависимости ставятся из uv.lock (`uv sync --frozen --no-dev`); в рантайме uv нет.
# Запуск: kd2-rules-mcp. Корень нужен только точке входа, чтобы отдать том кэша
# пользователю kd2; сам сервер работает не от root.

FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python3

COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python3

FROM python:3.12-slim

WORKDIR /app

COPY --from=build /app/.venv /app/.venv
COPY src ./src
COPY pyproject.toml uv.lock ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KD2_HOST=0.0.0.0 \
    KD2_PORT=8060 \
    KD2_CACHE_DIR=/data/cache \
    KD2_WORKSPACE=/data/workspace

RUN useradd --uid 1000 --create-home --user-group kd2 \
    && mkdir -p /data/cache /data/workspace \
    && chown kd2:kd2 /data /data/cache /data/workspace \
    && printf '%s\n' \
        '#!/bin/sh' \
        'set -eu' \
        'mkdir -p /data/cache /data/workspace' \
        'chown kd2:kd2 /data/cache' \
        'chown kd2:kd2 /data/workspace 2>/dev/null || true' \
        'exec setpriv --reuid=1000 --regid=1000 --init-groups --inh-caps=-all /app/.venv/bin/kd2-rules-mcp' \
        > /usr/local/bin/docker-entrypoint \
    && chmod 755 /usr/local/bin/docker-entrypoint \
    && command -v setpriv >/dev/null

EXPOSE 8060

ENTRYPOINT ["docker-entrypoint"]
