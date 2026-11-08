# 单镜像多入口：通过 SERVICE_NAME 选择启动的 FastAPI 应用。
# 生产环境应拆分为每服务独立镜像，这里保留一份以便本地一键启动。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 构建期 apt 源可覆盖：默认仍走 Debian 官方源，受限网络可传内网镜像源，
# 否则单是安装 curl 就可能在 deb.debian.org 上等到超时。
ARG APT_MIRROR=
RUN if [ -n "${APT_MIRROR}" ] && [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s#http://deb.debian.org#${APT_MIRROR}#g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY packages ./packages
COPY services ./services
COPY migrations ./migrations

# 构建期 pip 源可覆盖：默认仍走官方 pypi，受限网络可在构建时传内网镜像源，
# 避免依赖安装卡在 pypi.org 超时。参数只作用于构建阶段，不写入运行环境。
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=
RUN set -eux; \
    PIP_ARGS="--index-url ${PIP_INDEX_URL}"; \
    if [ -n "${PIP_TRUSTED_HOST}" ]; then PIP_ARGS="${PIP_ARGS} --trusted-host ${PIP_TRUSTED_HOST}"; fi; \
    pip install --upgrade pip ${PIP_ARGS} && pip install -e . ${PIP_ARGS}

ARG SERVICE_NAME=gateway
ENV SERVICE_NAME=${SERVICE_NAME}

EXPOSE 8000

# 启动前执行数据库迁移：仅带关系库的服务有 migrations/<svc>/ 目录，
# gateway/harness 无该目录时自动跳过，避免无库服务误跑 Alembic。
CMD ["sh", "-c", "if [ -f migrations/${SERVICE_NAME}/alembic.ini ]; then alembic -c migrations/${SERVICE_NAME}/alembic.ini upgrade head; fi && uvicorn ${SERVICE_NAME}.app:app --host 0.0.0.0 --port 8000"]
