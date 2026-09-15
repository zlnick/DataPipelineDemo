# -*- coding: utf-8 -*-
"""验证「BP 能否读到自己的 Ens 配置项名」（一条管道一个 BP 方案的前提）。

方案 A（一条数据管道一个 BP）：同名类 `demo.TransformProcess` 被实例化为多个
Ens.Config.Item（BP 名各异，如 TransformProcess__sql2soap），每个实例靠
`^demo.Config("bp", ..%ConfigName)` 读自己的 mapping/target_type，从而天然隔离。

本脚本核实：
  ① Ens.Host 是否存在 `%ConfigName` 属性（业务主机读自己 Item 名的官方途径）；
  ② `%Dictionary.CompiledProperty` 可见性；
  ③ 若可用，给出 BP 内读取示例。

用法（容器内）：
    cd tools/datakit && ./run.sh check_bp_configname.py
"""
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("check_bp_configname")


def main():
    from backend.services import iris_connector
    conn = iris_connector.get_connection()
    try:
        cmv = iris_connector.class_method_value
        for cls in ("Ens.Host", "Ens.BusinessProcess", "demo.TransformProcess"):
            for prop in ("%ConfigName", "ConfigName"):
                try:
                    exists = cmv("%Dictionary.CompiledProperty", "%ExistsId",
                                 f"{cls}||{prop}")
                except Exception as exc:  # noqa: BLE001
                    exists = f"ERR {str(exc)[:80]}"
                log.info("属性存在? %-22s %-14s -> %s", cls, prop, exists)
        # 直接读一次签名：确认类型与可访问性
        try:
            typ = cmv("%Dictionary.CompiledProperty", "Type", "Ens.Host||%ConfigName")
            who = cmv("%Dictionary.CompiledProperty", "Origin", "Ens.Host||%ConfigName")
            log.info("Ens.Host||%%ConfigName  Type=%s  Origin=%s", typ, who)
        except Exception as exc:  # noqa: BLE001
            log.info("读取属性元数据失败（可能不存在）: %s", str(exc)[:160])
        log.info("\nBP 内用法示例（每管道独立配置）：")
        log.info('  Set tCfgJson = $Get(^demo.Config("bp", ..%ConfigName), "")')
    finally:
        conn.close()


if __name__ == "__main__":
    main()
