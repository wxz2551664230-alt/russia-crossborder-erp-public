from urllib.parse import urlsplit, urlunsplit
from erp.core.errors import AppError


def ai_endpoint(value: str, path: str = "/v1/chat/completions") -> str:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AppError("AI 接口地址无效", code="ai_endpoint_invalid")
    current = parsed.path.rstrip("/")
    if not current:
        current = path
    elif current.endswith("/v1"):
        current += path.removeprefix("/v1")
    elif not current.endswith(("/chat/completions", "/images/edits", "/images/generations", "/responses")):
        raise AppError("请填写 AI 服务根地址、/v1 地址或完整接口地址", code="ai_endpoint_invalid")
    return urlunsplit((parsed.scheme, parsed.netloc, current, "", ""))
