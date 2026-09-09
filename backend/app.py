"""AI 数据自动化转换 Demo - Flask 应用入口。"""

import logging

from flask import Flask
from flask_cors import CORS

from backend.config import Config
from backend.routes.agents import agents_bp
from backend.routes.ai import ai_bp
from backend.routes.datasources import datasources_bp
from backend.routes.mappings import mappings_bp
from backend.routes.mapping import mapping_bp
from backend.routes.pipelines import pipelines_bp
from backend.routes.targets import targets_bp
from backend.routes.domain_models import domain_bp
from backend.services import iris_connector
from backend.services.mock_soap import mock_bp
from backend.utils import success

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def create_app() -> Flask:
    """创建并配置 Flask 应用。"""
    app = Flask(__name__)
    CORS(app, resources={r"/api/*": {"origins": Config.CORS_ORIGINS.split(",")}})

    # 注册路由蓝图
    app.register_blueprint(datasources_bp)
    app.register_blueprint(targets_bp)
    app.register_blueprint(agents_bp)
    app.register_blueprint(ai_bp)
    app.register_blueprint(mappings_bp)
    app.register_blueprint(pipelines_bp)
    app.register_blueprint(domain_bp)
    app.register_blueprint(mapping_bp)

    # 模拟第三方 SOAP 服务（SOAP 目标演示闭环，非 /api 前缀不走 CORS 限制）
    app.register_blueprint(mock_bp)

    @app.get("/")
    def index():
        """根路径提示信息。"""
        return {
            "project": "AI 数据自动化转换 Demo",
            "description": "数据源 -> AI 智能匹配 -> 转换目标 -> 自动生成 IRIS 生产管道",
            "endpoints": {
                "health": "/api/health",
                "datasources": "/api/datasources",
                "targets": "/api/targets",
                "agents": "/api/agents",
                "ai": "/api/ai/recommend",
                "mappings": "/api/mappings",
                "pipelines": "/api/pipelines",
            },
        }

    @app.get("/api/health")
    def health():
        """健康检查：探测 IRIS 连通性。"""
        ok = iris_connector.ping()
        return success(
            {"iris": "connected" if ok else "disconnected"}
        ), 200 if ok else 503

    return app


app = create_app()


def main() -> None:
    """应用启动入口：先验证 IRIS 连接，再启动 Flask 服务。"""
    if iris_connector.ping():
        logger.info("IRIS 连接成功")
    else:
        logger.warning("IRIS 连接失败，服务仍将启动，但依赖数据库的接口会返回错误。")
    logger.info("Flask 服务启动: http://%s:%s", Config.HOST, Config.PORT)
    app.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG)


if __name__ == "__main__":
    main()
