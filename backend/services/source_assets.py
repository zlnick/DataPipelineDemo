"""源资产模型服务兼容入口。"""
from backend.services.domain_models import create_source_asset
from backend.services.repository import (get_source_asset, list_source_assets,
                                          save_source_asset)

__all__ = ["create_source_asset", "save_source_asset", "get_source_asset",
           "list_source_assets"]
