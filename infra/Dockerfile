# 单镜像多入口：通过 SERVICE_NAME 选择启动的 FastAPI 应用。
# 生产环境应拆分为每服务独立镜像，这里保留一份以便本地一键启动。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY packages ./packages
COPY services ./services
COPY migrations ./migrations

RUN pip install --upgrade pip && pip install -e .

ARG SERVICE_NAME=gateway
ENV SERVICE_NAME=${SERVICE_NAME}

EXPOSE 8000

# 启动前执行数据库迁移：仅带关系库的服务有 migrations/<svc>/ 目录，
# gateway/harness 无该目录时自动跳过，避免无库服务误跑 Alembic。
CMD ["sh", "-c", "if [ -f migrations/${SERVICE_NAME}/alembic.ini ]; then alembic -c migrations/${SERVICE_NAME}/alembic.ini upgrade head; fi && uvicorn ${SERVICE_NAME}.app:app --host 0.0.0.0 --port 8000"]
