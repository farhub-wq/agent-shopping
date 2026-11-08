.PHONY: up down logs test lint seed dev-harness dev-sandbox-runner dev-web

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

dev-web:
	cd web && npm install && npm run dev
