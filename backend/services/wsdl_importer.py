"""SOAP 目标接入：WSDL 导入型。

SOAP 目标登记为「WSDL 导入型」：读 WSDL → IRIS %SOAP.WSDL.Reader 生成
proxy client + 消息类 + Business Operation → 生成的 BO 类作为管道 Operation 组件。
关键条件（已验证，见知识库 SOAP-WSDL反向生成BO.md）：
MakeClient=1、MakeBusinessOperation=1、MakeEnsembleClasses 保持默认 0。
"""

import json
import logging

from backend.services import iris_connector

logger = logging.getLogger(__name__)

# WSDL 导入默认包（与知识库一致）
DEFAULT_PACKAGES = {
    "client_package": "demo.soap.client",
    "bo_package": "demo.soap.bo",
    "msg_package": "demo.soap.msg",
}


def import_soap_operation(wsdl: str, service: str = "default",
                          packages: dict | None = None) -> dict:
    """调 IRIS demo.WSDLImporter 导入 WSDL，生成 BO 并返回。

    参数:
        wsdl: WSDL 地址（http/https URL 或容器内文件路径）
        service: SOAP 服务名（用于命名组件 SOAPOp_{service} 与记录映射）
        packages: 覆盖默认生成包（client_package/bo_package/msg_package）

    返回: {"ok": bool, "boClass": str, "message": str}
    成功后记录 ^demo.Config("soap", service) = BO 类名，供后续复用与 TransformProcess 路由。
    """
    if not wsdl:
        return {"ok": False, "boClass": "", "message": "SOAP 目标需要提供 WSDL 地址"}
    pkgs = {**DEFAULT_PACKAGES, **(packages or {})}
    try:
        data = iris_connector.class_method_value(
            "demo.WSDLImporter", "Import",
            wsdl, pkgs["client_package"], pkgs["bo_package"], pkgs["msg_package"])
        result = json.loads(data or "{}")
    except Exception as exc:  # noqa: BLE001
        logger.error("WSDL 导入调用失败: %s", exc)
        return {"ok": False, "boClass": "", "message": str(exc)}
    if result.get("ok") and result.get("boClass"):
        _save_bo_mapping(service, result["boClass"])
        return {"ok": True, "boClass": result["boClass"], "message": ""}
    return {"ok": False, "boClass": "",
            "message": result.get("message", "WSDL 导入失败")}


def _save_bo_mapping(service: str, bo_class: str) -> None:
    """记录 service → BO 类映射到 ^demo.Config("soap", service)，并标记当前激活服务。"""
    import iris
    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        key = service or "default"
        native.set(bo_class, "^demo.Config", "soap", key)
        native.set(key, "^demo.Config", "soap", "active_service")
    finally:
        conn.close()


def get_bo_class(service: str = "default") -> str:
    """读取已记录的 service → BO 类映射（空字符串表示未记录）。"""
    import iris
    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        return native.getString("^demo.Config", "soap", service or "default")
    finally:
        conn.close()
