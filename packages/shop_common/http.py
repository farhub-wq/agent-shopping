from typing import Any

import httpx

from .errors import ApiError
from .logging import get_logger

logger = get_logger(__name__)


class ServiceClient:
    """服务间调用：固定超时、幂等重试、错误归一。

    只对 5xx 和网络错误重试，4xx 直接上抛——4xx 重试不会让结果变好，只会放大故障。
    """

    def __init__(self, base_url: str, *, timeout: float = 5.0, retries: int = 2) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        # 仅 5xx 与网络异常会进入重试；4xx 已在下面直接抛 ApiError，不会落到这里。
        for attempt in range(self.retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.request(method, url, json=json, headers=headers)
                if response.status_code >= 500:
                    # 5xx 转成 HTTPStatusError，借统一异常分支触发重试。
                    raise httpx.HTTPStatusError(
                        f"上游 {response.status_code}", request=response.request, response=response
                    )
                if response.status_code >= 400:
                    detail = _safe_json(response)
                    # 上游错误码不在本地映射表时统一归为依赖失败，避免调用方拿到陌生 code。
                    code = str(detail.get("code") or "DEPENDENCY_FAILED")
                    if code not in ApiError.status_by_code:
                        code = "DEPENDENCY_FAILED"
                    raise ApiError(
                        code,
                        detail.get("message", f"{url} 调用失败"),
                        details={"status": response.status_code, "upstream": detail},
                    )
                return _safe_json(response)
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_error = exc
                logger.warning("调用上游失败 attempt=%s url=%s error=%s", attempt + 1, url, exc)
        raise ApiError(
            "DEPENDENCY_FAILED",
            f"{url} 不可用",
            details={"error": type(last_error).__name__ if last_error else "unknown"},
        )


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    # 上游返回非 JSON（如网关 HTML 错误页）时兜底成 message，避免解析异常掩盖真实错误。
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"data": data}
    except ValueError:
        return {"message": response.text[:200]}
