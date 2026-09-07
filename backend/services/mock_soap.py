"""模拟第三方 SOAP 服务（仅供 demo 演示闭环）——写入型 AddPatient。

用途：SOAP 转换目标的 BO（WSDL 反向生成客户端）会向被调 SOAP 系统发起调用，
本模块用 Python 模拟该第三方 HIS 的「写入」接口：
接收 SOAP Envelope（AddPatientRequest，内含数据管道转换好的 PatientEntity 实体）→
保存实体（写入 PatientEntity 演示表）→ 返回 AddPatientResponse 回执（Code/Message）。

这样无需真实第三方系统 / 无需改 IRIS Web Application，即可让
SQL 源 → SOAP 目标的管道看到「转换产物（三字段实体）→ 远端接收成功」的完整流转。
"""

import logging
import re

from flask import Blueprint, Response, request

from backend.services import iris_connector

logger = logging.getLogger(__name__)

# 与内置示例 WSDL（写入型 patient.wsdl）一致的命名空间
SOAP_NS = "http://demo.soap/PatientService"

mock_bp = Blueprint("mock_soap", __name__)


def _extract_field(body: str, tag: str) -> str:
    """从 SOAP Envelope 文本中按元素名提取字段文本值。"""
    match = re.search(rf"<{tag}[^>]*>\s*([^<]+?)\s*<", body)
    return match.group(1).strip() if match else ""


def _xml_escape(text: str) -> str:
    """对进入 XML 文本的值做基础转义。"""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@mock_bp.route("/mock/soap/PatientService", methods=["POST"])
def mock_add_patient():
    """模拟第三方 SOAP 服务 AddPatient：接收患者实体并返回回执。

    请求为 SOAP 1.1 Envelope（document/literal），结构：
      AddPatientRequest（xmlns=http://demo.soap/PatientService）
        <Patient><PatientNo/><FullName/><Gender/></Patient>
    数据管道转换后的目标行（三字段实体）就在这里被远端接收。

    响应：AddPatientResponse → <Result><Code>0</Code><Message>OK</Message></Result>。
    """
    body = request.get_data(as_text=True) or ""
    patient_no = _extract_field(body, "PatientNo")
    full_name = _extract_field(body, "FullName")
    gender = _extract_field(body, "Gender")

    # 模拟第三方 HIS 保存实体：写入 PatientEntity 演示表（init_data 已建，幂等）
    try:
        iris_connector.execute(
            "INSERT OR UPDATE INTO PatientEntity (PatientNo, FullName, Gender) VALUES (?, ?, ?)",
            [patient_no, full_name, gender])
    except Exception as exc:  # noqa: BLE001 - mock 不因保存失败而中断应答
        logger.warning("mock SOAP 保存 PatientEntity 失败（表可能未建）: %s", exc)

    logger.info("mock SOAP AddPatient 接收实体: PatientNo=%s FullName=%s Gender=%s",
                patient_no, full_name, gender)

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">\n'
        '  <soap:Body>\n'
        f'    <AddPatientResponse xmlns="{SOAP_NS}">\n'
        '      <Result>\n'
        '        <Code>0</Code>\n'
        '        <Message>OK</Message>\n'
        '      </Result>\n'
        '    </AddPatientResponse>\n'
        '  </soap:Body>\n'
        '</soap:Envelope>\n'
    )
    return Response(xml, content_type="text/xml; charset=utf-8")

