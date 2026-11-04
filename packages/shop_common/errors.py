from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """业务错误统一出口：错误码稳定，HTTP 状态码映射集中在这里。"""

    status_by_code = {
        "BAD_REQUEST": 400,
        "UNAUTHORIZED": 401,
        "FORBIDDEN": 403,
        "NOT_FOUND": 404,
        "CONFLICT": 409,
        "IDEMPOTENCY_KEY_REUSED": 409,
        "RATE_LIMITED": 429,
        "DEPENDENCY_FAILED": 503,
        "INTERNAL": 500,
    }

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.status_code = self.status_by_code.get(code, 500)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def install_error_handlers(app: FastAPI, service_name: str) -> None:
    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL",
                "message": f"{service_name} 内部错误",
                "details": {"error": type(exc).__name__},
            },
        )
