# -*- coding: utf-8 -*-
"""零破坏验证「一条数据管道一个转换 BP」：拓扑 + IRIS 渲染文本。

验证点（不写库、不 Load、不动现有 Production）：
  1. 多管道拓扑里每组各有一个转换 BP（`TransformProcess__<管道类别>`，className 均为 demo.TransformProcess）；
  2. 源 BS 的 `TargetConfigNames` 指向**本组自己的** BP（管道之间不互相投递）；
  3. `shared` 类别只剩 JavaGateway；
  4. `demo.PipelineGenerator.RenderProduction(拓扑)` 的渲染文本里出现**两个同名类不同名 Item**
     （Ens 组件身份 = Item 名，class 可复用）——即 IRIS 侧确认支持一管道一 BP。

用法（容器内，推荐走 datakit 入口）：
    cd tools/datakit && ./run.sh verify_multi_bp_pipeline.py
"""
import json
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("verify_multi_bp")

GROUPS = [
    {"source_type": "SQL", "target_type": "SOAP", "_category": "sql2soap",
     "source_config": {"dsn": "CLINIC", "query": "SELECT * FROM CLINIC.Patient",
                       "key_field": "MRN"},
     "target_config": {"service": "PatientService"}},
    {"source_type": "SQL", "target_type": "DB", "_category": "sql2db",
     "source_config": {"dsn": "CLINIC", "query": "SELECT * FROM CLINIC.Patient",
                       "key_field": "MRN"},
     "mappings": [{"id": "M_VERIFY", "target_table": "Patient",
                   "field_mappings": [{"source": "Name", "target": "Name"}]}]},
]


def _tcn(comp) -> str | None:
    """取组件 settings 里 Host/TargetConfigNames 的值。"""
    for s in comp.get("settings") or []:
        if s.get("name") == "TargetConfigNames":
            return s.get("value")
    return None


def main() -> None:
    from backend.routes.pipelines import build_multi_pipeline_topology
    from backend.services import iris_connector as ic

    topo = build_multi_pipeline_topology(GROUPS)
    comps = topo["components"]
    log.info("== 拓扑（%d 组件）==", len(comps))
    for c in comps:
        log.info("  [%s] %-34s %s", c.get("category"), c.get("name"), c.get("className"))

    bps = [c for c in comps if c.get("type") == "TransformProcess"]
    assert len(bps) == 2, f"应有两组各自的 BP: {[c['name'] for c in bps]}"
    assert {c["name"] for c in bps} == {"TransformProcess__sql2soap", "TransformProcess__sql2db"}, bps
    assert all(c["className"] == "demo.TransformProcess" for c in bps), "BP 应复用同一个类"
    assert all(c.get("category") != "shared" for c in bps), "转换 BP 不应再标 shared"
    shared = [c["name"] for c in comps if c.get("category") == "shared"]
    assert shared == ["EnsLib.JavaGateway.Service"], f"shared 应只有 JavaGateway: {shared}"

    # 源 BS → 自己的 BP
    pairs = {}
    for c in comps:
        if c.get("type") == "SQLService":
            pairs[c["name"]] = _tcn(c)
    log.info("\n== 源 BS → BP 投递 ==")
    for bs, bp in sorted(pairs.items()):
        log.info("  %s -> %s", bs, bp)
    assert sorted(pairs.values()) == ["TransformProcess__sql2db", "TransformProcess__sql2soap"], pairs
    assert not any(v == "TransformProcess" for v in pairs.values()), "源 BS 仍指向旧的共享 BP"

    # IRIS 渲染（只取文本，不落盘/不启动）
    topo_render = dict(topo)
    topo_render["production"] = "demo.TmpMultiBpVerify"   # 仅用于文本检查，不 Load
    src = ic.class_method_value("demo.PipelineGenerator", "RenderProduction",
                                json.dumps(topo_render, ensure_ascii=False))
    assert src, "RenderProduction 返回空"
    log.info("\n== IRIS 渲染文本中的 BP Item ==")
    import re
    for m in re.finditer(r'<Item Name="(TransformProcess[^"]*)"[^>]*?ClassName="([^"]+)"', str(src)):
        log.info('  Item Name="%s" ClassName="%s"', m.group(1), m.group(2))
    assert 'Name="TransformProcess__sql2soap"' in src, "渲染文本缺少 sql2soap 的 BP Item"
    assert 'Name="TransformProcess__sql2db"' in src, "渲染文本缺少 sql2db 的 BP Item"
    assert src.count('ClassName="demo.TransformProcess"') == 2, "应渲染出两个同类 BP Item"
    log.info("\n✓ 一管道一 BP：拓扑归属 / 源 BS 投递 / 共享仅 JavaGateway / IRIS 渲染全部通过")


if __name__ == "__main__":
    main()
