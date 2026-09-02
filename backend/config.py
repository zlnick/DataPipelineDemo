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

