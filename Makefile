.PHONY: up down logs test lint seed dev-harness dev-sandbox-runner dev-web setup-sandbox-host setup-sandbox-node build-sandbox-image sandbox-gvisor-regression

up:
	docker compose up --build -d

down:
	docker compose down -v

logs:
	docker compose logs -f --tail=100

test:
	pytest -q

lint:
	ruff check packages services tests

# 本地不依赖 Docker 的快速验证：使用内存/离线依赖
dev-harness:
	PYTHONPATH=. SANDBOX_ENABLED=false uvicorn harness.app:app --reload --port 8006

# 独立沙箱 runner：本机需要可用的 Docker 守护进程；harness 通过 SANDBOX_RUNNER_URL 调用
dev-sandbox-runner:
	PYTHONPATH=. uvicorn sandbox_runner.app:app --reload --port 8011

# 构建端点必须与 runner 连接的守护进程一致：生产 runner 走 rootless dockerd（独立镜像存储），
# 镜像构建进默认 root 守护进程时 runner 会一直 ImageNotFound，因此允许显式指定端点。
SANDBOX_DOCKER_HOST ?=
DOCKER_CMD = docker $(if $(SANDBOX_DOCKER_HOST),-H $(SANDBOX_DOCKER_HOST),)

# 构建沙箱镜像并打印内容寻址的 SANDBOX_IMAGE；生产必须用输出值替换 :latest
build-sandbox-image:
	$(DOCKER_CMD) build -t shop-agent-sandbox:latest infra/sandbox
	@echo "构建端点：$(if $(SANDBOX_DOCKER_HOST),$(SANDBOX_DOCKER_HOST),docker 默认端点)"
	@echo "SANDBOX_IMAGE=$$($(DOCKER_CMD) image inspect --format '{{.Id}}' shop-agent-sandbox:latest)"

# 宿主侧一次性初始化：rootless dockerd + gVisor（需 root，仅 Debian/Ubuntu，幂等）
setup-sandbox-host:
	sudo bash infra/rootless-docker/setup.sh

# 专用沙箱节点初始化：rootful dockerd + gVisor（节点不得承载业务容器与业务数据）。
# 用 -E 透传 SANDBOX_SETUP_MODE，脚本据此走 dedicated 分支；可选 DOCKER_REGISTRY_MIRRORS。
setup-sandbox-node:
	SANDBOX_SETUP_MODE=dedicated sudo -E bash infra/rootless-docker/setup.sh

# gVisor 兼容性回归：逐个 runner 校验 runsc 后跑标准库/线程/子进程/文件等探针。
# 需要 SANDBOX_RUNNER_URLS（逗号分隔）与 RUNNER_SIGNING_SECRET；可选 SANDBOX_PROBE_MODULES=numpy,pandas
sandbox-gvisor-regression:
	python infra/sandbox/gvisor_regression.py

dev-web:
	cd web && npm install && npm run dev
