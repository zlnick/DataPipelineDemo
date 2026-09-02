"""分层领域模型服务：校验模型并委托 repository 持久化。"""

from backend.schemas.models import SourceAssetModel, TargetInterfaceModel, TransformationPlan
from backend.services import repository


def create_source_asset(payload: dict) -> dict:
    return repository.save_source_asset(SourceAssetModel(**payload))


def create_target_interface(payload: dict) -> dict:
    return repository.save_target_interface(TargetInterfaceModel(**payload))


def create_transformation_plan(payload: dict) -> dict:
    return repository.save_transformation_plan(TransformationPlan(**payload))


def verify_transformation_plan(plan: dict) -> dict:
    """事实验证入口的纯服务实现，避免 API 直接依赖存储细节。"""
    from backend.services import pipeline_validator, transformation_validator
    mappings = plan.get("mappings") or plan.get("recommendations") or []
    mappings = transformation_validator.normalize_recommendations(mappings)
    assets = plan.get("source_models") or plan.get("assets") or []
    return pipeline_validator.run_transformation_validation(mappings, assets)
