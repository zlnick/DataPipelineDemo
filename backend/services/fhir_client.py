"""FHIR endpoint HTTP 客户端（带 Basic Auth）。

注意（方案 A）：IRIS FHIR Server 资源读写默认要求 Basic Auth（superuser/SYS），
仅 /metadata 匿名公开；所有请求显式携带 Accept: application/fhir+json（否则返回 406）。
"""

import base64
import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


def _request(url: str, username: str, password: str,
             payload: dict | None = None, method: str | None = None) -> dict:
    """发起带 Basic Auth 的 FHIR 请求并返回 JSON。

    参数:
        url: 完整请求 URL。
        username: Basic Auth 用户名。
        password: Basic Auth 密码。
        payload: 可选的 JSON 请求体（POST）。
        method: HTTP 方法。

    返回:
        响应 JSON（dict）。
    """
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = method or "POST"
    req = urllib.request.Request(url, data=data, method=method)
    # 必须显式设置 Accept 头，否则 IRIS FHIR Server 返回 406
    req.add_header("Accept", "application/fhir+json")
    req.add_header("Authorization", f"Basic {token}")
    if payload is not None:
        req.add_header("Content-Type", "application/fhir+json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.error("FHIR 请求失败 %s %s: %s", method or "GET", url, exc.code)
        raise


def get_capability_statement(base_url: str, username: str, password: str) -> dict:
    """拉取 FHIR endpoint 的 CapabilityStatement（/metadata，匿名可访问）。

    参数:
        base_url: FHIR endpoint 基础 URL（含结尾 /）。
        username: Basic Auth 用户名。
        password: Basic Auth 密码。

    返回:
        CapabilityStatement JSON。
    """
    return _request(base_url.rstrip("/") + "/metadata", username, password)


def search_resources(base_url: str, username: str, password: str,
                     resource_type: str) -> dict:
    """搜索指定资源类型（返回 Bundle）。

    参数:
        base_url: FHIR endpoint 基础 URL（含结尾 /）。
        username: Basic Auth 用户名。
        password: Basic Auth 密码。
        resource_type: 资源类型（如 Patient）。

    返回:
        Bundle JSON。
    """
    return _request(base_url.rstrip("/") + "/" + resource_type, username, password)


def get_resource(base_url: str, username: str, password: str,
                 resource_type: str, resource_id: str) -> dict:
    """按 ID 获取单个 FHIR 资源。

    参数:
        base_url: FHIR endpoint 基础 URL（含结尾 /）。
        username: Basic Auth 用户名。
        password: Basic Auth 密码。
        resource_type: 资源类型（如 Patient）。
        resource_id: 资源 ID。

    返回:
        资源 JSON。
    """
    return _request(
        f"{base_url.rstrip('/')}/{resource_type}/{resource_id}", username, password)


def submit_bundle(base_url: str, username: str, password: str,
                  entries: list[dict]) -> dict:
    """以 Bundle(transaction) 方式提交资源到 FHIR endpoint（写入示例/模拟数据）。

    参数:
        base_url: FHIR endpoint 基础 URL（含结尾 /）。
        username: Basic Auth 用户名。
        password: Basic Auth 密码。
        entries: FHIR 资源对象列表。

    返回:
        FHIR 服务器返回的 Bundle 响应（dict）。
    """
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {"resource": e, "request": {"method": "PUT", "url": f"{e['resourceType']}/{e['id']}"}}
            for e in entries
        ],
    }
    return _request(base_url.rstrip("/") + "/", username, password, payload=bundle)
