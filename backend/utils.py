"""通用工具模块。"""

from __future__ import annotations

from typing import Any


def success(data: Any = None, message: str = "success") -> dict:
    """构造统一格式的成功响应。

    参数:
        data: 响应数据。
        message: 提示信息。

    返回:
        统一 JSON 响应字典。
    """
    return {"code": 0, "data": data, "message": message}


def error(message: str = "error", code: int = 1, data: Any = None) -> dict:
    """构造统一格式的失败响应。

    参数:
        message: 错误信息。
        code: 错误码（非 0 表示失败）。
        data: 附加数据。

    返回:
        统一 JSON 响应字典。
    """
    return {"code": code, "data": data, "message": message}
