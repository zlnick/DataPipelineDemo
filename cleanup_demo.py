"""清理预置演示数据（供现场从零演示）。"""
import iris
from backend.services import iris_connector

DEMO_GLOBALS = [
    "^demo.DataSource", "^demo.DataAsset", "^demo.Target", "^demo.Mapping", "^demo.Config",
    "^demo.SourceAsset", "^demo.TargetInterface", "^demo.TransformationPlan", "^demo.ValidationIssue"
]
PRODUCTION_NAME = "demo.DataflowProduction"
GENERATED_FILE = "/dur/generated/DataflowProduction.cls"


def _clean_production():
    import os
    ic = iris_connector
    try:
        ic.class_method_value("Ens.Director", "StopProduction", PRODUCTION_NAME, 1)
    except Exception:
        pass
    try:
        ic.class_method_value("Ens.Config.Production", "%DeleteId", PRODUCTION_NAME)
    except Exception:
        pass
    try:
        ic.class_method_value("%SYSTEM.OBJ", "Delete", PRODUCTION_NAME, "c")
    except Exception:
        pass
    try:
        if os.path.exists(GENERATED_FILE):
            os.remove(GENERATED_FILE)
    except Exception:
        pass
    print("已清除动态生成的 Production")


def main():
    _clean_production()
    conn = iris_connector.get_connection()
    native = iris.createIRIS(conn)
    try:
        for g in DEMO_GLOBALS:
            native.kill(g)
            print(f"已清空 {g}")
    finally:
        conn.close()
    for table in ("PatientEntity", "PatientSource", "FHIRQueue", "Patient", "Observation"):
        iris_connector.execute(f"DELETE FROM {table}")
        print(f"目标表 {table} 数据已清空（表结构保留）")
    print("清理完成：演示可从零开始")


if __name__ == "__main__":
    main()
