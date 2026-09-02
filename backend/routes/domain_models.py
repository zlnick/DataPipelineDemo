"""源资产、目标接口及转换计划 API。"""

from flask import Blueprint, request

from backend.services import domain_models, repository
from backend.utils import error, success

domain_bp = Blueprint("domain_models", __name__)


def _post(factory, label):
    body = request.get_json(silent=True) or {}
    try:
        item = factory(body)
    except Exception as exc:
        return error(f"参数校验失败: {exc}"), 400
    return success(item, f"{label}已创建"), 201


@domain_bp.post("/api/source-assets")
@domain_bp.post("/api/source-models")
def create_source_asset():
    return _post(domain_models.create_source_asset, "源资产")


@domain_bp.get("/api/source-assets")
@domain_bp.get("/api/source-models")
def list_source_assets():
    return success({"items": repository.list_source_assets(request.args.get("source_id"))})


@domain_bp.get("/api/source-assets/<asset_id>")
@domain_bp.get("/api/source-models/<asset_id>")
def get_source_asset(asset_id):
    item = repository.get_source_asset(asset_id)
    return success(item) if item else (error("源资产不存在"), 404)


@domain_bp.post("/api/target-interfaces")
@domain_bp.post("/api/target-models")
def create_target_interface():
    return _post(domain_models.create_target_interface, "目标接口")


@domain_bp.get("/api/target-interfaces")
@domain_bp.get("/api/target-models")
def list_target_interfaces():
    return success({"items": repository.list_target_interfaces()})


@domain_bp.get("/api/target-interfaces/<interface_id>")
@domain_bp.get("/api/target-models/<interface_id>")
def get_target_interface(interface_id):
    item = repository.get_target_interface(interface_id)
    return success(item) if item else (error("目标接口不存在"), 404)


@domain_bp.post("/api/transformation-plans")
def create_transformation_plan():
    return _post(domain_models.create_transformation_plan, "转换计划")


@domain_bp.get("/api/transformation-plans")
def list_transformation_plans():
    return success({"items": repository.list_transformation_plans()})


@domain_bp.get("/api/transformation-plans/<plan_id>")
def get_transformation_plan(plan_id):
    item = repository.get_transformation_plan(plan_id)
    return success(item) if item else (error("转换计划不存在"), 404)


@domain_bp.post("/api/transformation-plans/<plan_id>/verify")
@domain_bp.post("/api/transformation-plans/<plan_id>/validate")
def verify_transformation_plan(plan_id):
    plan = repository.get_transformation_plan(plan_id)
    if not plan:
        return error("转换计划不存在"), 404
    return success(domain_models.verify_transformation_plan(plan), "转换计划事实验证完成")
