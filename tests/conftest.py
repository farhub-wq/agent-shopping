import os

os.environ.setdefault("SERVICE_NAME", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/test.db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
