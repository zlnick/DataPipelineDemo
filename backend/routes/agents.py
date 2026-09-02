"""已封装 AI Agent 目录 API。"""

from flask import Blueprint

from backend.services import agents as agent_registry
from backend.utils import success

agents_bp = Blueprint("agents", __name__, url_prefix="/api/agents")


@agents_bp.get("")
def list_agents():
    """已封装 Agent 列表（前端「AI Agents」页）。"""
    return success({"items": agent_registry.list_agents()})
