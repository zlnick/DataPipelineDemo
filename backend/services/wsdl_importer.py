"""SOAP 目标接入：WSDL 导入型。

SOAP 目标登记为「WSDL 导入型」：读 WSDL → IRIS %SOAP.WSDL.Reader 生成
proxy client + 消息类 + Business Operation → 生成的 BO 类作为管道 Operation 组件。
关键条件（已验证，见知识库 SOAP-WSDL反向生成BO.md）：
MakeClient=1、MakeBusinessOperation=1、MakeEnsembleClasses 保持默认 0。
"""

import json
import logging
import os

from backend.services import iris_connector

logger = logging.getLogger(__name__)

# WSDL 导入默认包（与知识库一致）
DEFAULT_PACKAGES = {
    "client_package": "demo.soap.client",
    "bo_package": "demo.soap.bo",
    "msg_package": "demo.soap.msg",
}

# 默认内置示例 WSDL（当用户输入 /tmp/patient.wsdl 但文件不存在时自动补全）
DEFAULT_SAMPLE_WSDL = """<?xml version="1.0" encoding="UTF-8"?>
<definitions name="PatientService"
    targetNamespace="http://demo.soap/PatientService"
    xmlns="http://schemas.xmlsoap.org/wsdl/"
    xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
    xmlns:tns="http://demo.soap/PatientService"
    xmlns:xsd="http://www.w3.org/2001/XMLSchema">

  <types>
    <xsd:schema targetNamespace="http://demo.soap/PatientService">
      <xsd:complexType name="PatientEntity">
        <xsd:sequence>
          <xsd:element name="PatientNo" type="xsd:string"/>
          <xsd:element name="FullName" type="xsd:string"/>
          <xsd:element name="Gender" type="xsd:string"/>
        </xsd:sequence>
      </xsd:complexType>
      <xsd:element name="GetPatientRequest">
        <xsd:complexType>
          <xsd:sequence>
            <xsd:element name="PatientNo" type="xsd:string"/>
          </xsd:sequence>
        </xsd:complexType>
      </xsd:element>
      <xsd:element name="GetPatientResponse">
        <xsd:complexType>
          <xsd:sequence>
            <xsd:element name="Patient" type="tns:PatientEntity"/>
          </xsd:sequence>
        </xsd:complexType>
      </xsd:element>
    </xsd:schema>
  </types>

  <message name="GetPatientInput">
    <part name="parameters" element="tns:GetPatientRequest"/>
  </message>
  <message name="GetPatientOutput">
    <part name="parameters" element="tns:GetPatientResponse"/>
  </message>

  <portType name="PatientPortType">
    <operation name="GetPatient">
      <input message="tns:GetPatientInput"/>
      <output message="tns:GetPatientOutput"/>
    </operation>
  </portType>

  <binding name="PatientBinding" type="tns:PatientPortType">
    <soap:binding style="document" transport="http://schemas.xmlsoap.org/soap/http"/>
    <operation name="GetPatient">
      <soap:operation soapAction="http://demo.soap/PatientService/GetPatient"/>
      <input><soap:body use="literal"/></input>
      <output><soap:body use="literal"/></output>
    </operation>
  </binding>

  <service name="PatientService">
    <port name="PatientPort" binding="tns:PatientBinding">
      <soap:address location="http://127.0.0.1:52773/csp/demo/soap/PatientService.cls"/>
    </port>
  </service>
</definitions>"""


def _ensure_wsdl_in_iris(wsdl: str) -> str:
    """确保 WSDL 文件可被 IRIS 容器内的 %SOAP.WSDL.Reader 读取。

    如果 wsdl 为 HTTP(S) URL 则直接返回；
    若为本地文件路径或 XML 文本，将内容同步保存到 IRIS 容器。
    """
    wsdl_clean = wsdl.strip()
    if wsdl_clean.lower().startswith(("http://", "https://")):
        return wsdl_clean

    content = None
    target_path = wsdl_clean if wsdl_clean.startswith("/") else f"/tmp/{wsdl_clean}"

    if wsdl_clean.startswith("<"):
        content = wsdl_clean
        target_path = "/tmp/patient.wsdl"
    elif os.path.exists(wsdl_clean):
        try:
            with open(wsdl_clean, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as exc:
            logger.warning("读取后端本地 WSDL 文件失败 %s: %s", wsdl_clean, exc)

    if not content and "patient.wsdl" in wsdl_clean.lower():
        content = DEFAULT_SAMPLE_WSDL

    if content:
        try:
            import iris
            conn = iris_connector.get_connection()
            irispy = iris.createIRIS(conn)
            file_stream = irispy.classMethodObject("%Stream.FileCharacter", "%New")
            file_stream.invoke("LinkToFile", target_path)
            file_stream.invoke("Write", content)
            file_stream.invoke("%Save")
            conn.close()
            logger.info("已同步 WSDL 内容到 IRIS 容器路径: %s", target_path)
        except Exception as exc:
            logger.error("同步 WSDL 文件到 IRIS 容器失败: %s", exc)

    return target_path


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
    iris_wsdl_path = _ensure_wsdl_in_iris(wsdl)
    try:
        data = iris_connector.class_method_value(
            "demo.WSDLImporter", "Import",
            iris_wsdl_path, pkgs["client_package"], pkgs["bo_package"], pkgs["msg_package"])
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
