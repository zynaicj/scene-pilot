"""单次模型连通性测试，仅输出经过过滤的状态码和错误类型。"""
import re
from scene_pilot.core.config import get_settings
from scene_pilot.core.llm import create_qwen_model


def safe_error_details(exc: Exception) -> dict:
    """不展示密钥、请求体、原始异常文本或第三方请求 URL。"""
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        cause = getattr(exc, "__cause__", None)
        body = getattr(cause, "body", None)
        if not isinstance(body, dict):
            body = {}
        if status is None:
            status = getattr(cause, "status_code", None)
    error = body.get("error", body)
    code = error.get("code") if isinstance(error, dict) else None
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
        code = "unknown"
    return {
        "exception_type": type(exc).__name__,
        "http_status": status if isinstance(status, int) and 100 <= status <= 599 else None,
        "provider_code": code,
    }


def main() -> None:
    settings = get_settings()
    print(f"测试模型：{settings.qwen_model}（会进行一次模型调用）")
    try:
        model = create_qwen_model(settings)
        response = model.invoke("你好，只回复 OK。")
    except Exception as exc:
        details = safe_error_details(exc)
        print(f"模型调用失败：{details}")
        if details["http_status"] == 403:
            print("请核对百炼模型权限、账户免费额度与业务空间；不要为了排障取消费用保护。")
        elif details["http_status"] == 500:
            print("模型服务返回 500，请稍后重试或核对模型与 API 地址。")
        return
    print(f"模型连接正常：{str(response.content)[:60]}")


if __name__ == "__main__":
    main()
