"""目标接口模型服务兼容入口。"""
from backend.services.domain_models import create_target_interface
from backend.services.repository import (get_target_interface,
                                          list_target_interfaces,
                                          save_target_interface)

__all__ = ["create_target_interface", "save_target_interface",
           "get_target_interface", "list_target_interfaces"]
