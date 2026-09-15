"""应用配置模块。

使用 python-dotenv 加载项目根目录的 .env 文件。
注意：load_dotenv 默认不覆盖已存在的环境变量，
因此 docker-compose 注入的环境变量优先于 .env 文件。
"""

import os

from dotenv import load_dotenv

# 项目根目录（本文件位于 <root>/backend/ 下，向上两级）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 加载 .env（本地开发）；已存在的环境变量不会被覆盖
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))


class Config:
    """Flask 应用配置。"""

    DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    HOST = os.getenv("FLASK_HOST", "0.0.0.0")
    PORT = int(os.getenv("FLASK_PORT", "5000"))
    # 允许跨域访问的前端来源（逗号分隔）
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173")
    # 演示用第三方 SOAP 服务地址（backend 的 Python mock，见 services/mock_soap.py）。
    # SOAP 目标的 BO（WSDL 反向生成客户端）在 IRIS 容器内，故用 docker 网络内地址 dataflow-backend。
    MOCK_SOAP_URL = os.getenv("MOCK_SOAP_URL", "http://dataflow-backend:5000/mock/soap/PatientService")


class IRISConfig:
    """IRIS 连接参数配置，从环境变量读取。"""

    HOST = os.getenv("IRIS_HOST", "127.0.0.1")
    PORT = int(os.getenv("IRIS_PORT", "1972"))
    NAMESPACE = os.getenv("IRIS_NAMESPACE", "USER")
    USERNAME = os.getenv("IRIS_USERNAME", "superuser")
    PASSWORD = os.getenv("IRIS_PASSWORD", "SYS")
    TIMEOUT = int(os.getenv("IRIS_TIMEOUT", "10"))

    @classmethod
    def as_dict(cls) -> dict:
        """返回 iris.connect 所需的参数字典。"""
        return {
            "hostname": cls.HOST,
            "port": cls.PORT,
            "namespace": cls.NAMESPACE,
            "username": cls.USERNAME,
            "password": cls.PASSWORD,
            "timeout": cls.TIMEOUT,
        }


class LLMConfig:
    """LLM（OpenAI 兼容接口）配置。

    - BASE_URL: 任意 OpenAI 兼容服务的 base_url（DeepSeek / 通义 / 智谱 / Kimi 等）
    - API_KEY:  服务商提供的密钥
    - MODEL:    模型名称
    """

    BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    API_KEY = os.getenv("LLM_API_KEY", "")
    MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
    # 单次响应上限（Agent 生成 BP 类源码等长输出需要较大值；截断会被显式判失败）
    MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "64000"))


class FHIRConfig:
    """FHIR 数据源（IRIS 自带 FHIR Server）配置。

    注意（方案 A）：IRIS FHIR Server 对资源读写默认要求 Basic Auth（superuser/SYS），
    仅 /metadata 匿名公开；本配置提供访问 endpoint 所需的认证信息。
    """

    # FHIR endpoint 基础 URL（含结尾 /）
    BASE_URL = os.getenv(
        "FHIR_BASE_URL",
        "http://127.0.0.1:52773/csp/healthshare/fhirserver/fhir/r4/",
    )
    USERNAME = os.getenv("FHIR_USERNAME", "superuser")
    PASSWORD = os.getenv("FHIR_PASSWORD", "SYS")


def to_internal_url(url: str) -> str:
    """把「浏览器视角」的回环地址换成容器内可达的 IRIS 主机名。

    演示程序里数据源/目标登记时通常填 http://localhost:52773/...（浏览器视角），
    但 backend 与 IRIS 分属不同容器，必须走 docker 网络服务名（IRIS_HOST，默认 iris）
    才能访问 IRIS 的 FHIR / SOAP 端点；否则生成后的校验、探查会 Connection refused，
    导致 FHIR 目标落地效果检查误报（曾使第二条管道生成被判失败）。

    非回环地址（含已在容器网络内的主机名）原样返回。
    """
    text = str(url or "").strip()
    if not text:
        return text
    internal = (os.getenv("IRIS_INTERNAL_HOST") or "iris").strip() or "iris"
    for loopback in ("localhost", "127.0.0.1", "0.0.0.0"):
        text = text.replace(f"://{loopback}:", f"://{internal}:")
        text = text.replace(f"://{loopback}/", f"://{internal}/")
    return text

