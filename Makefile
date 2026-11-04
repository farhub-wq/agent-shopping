.PHONY: up down logs test lint seed dev-harness dev-web

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

dev-web:
	cd web && npm install && npm run dev
