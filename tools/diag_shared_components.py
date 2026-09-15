# -*- coding: utf-8 -*-
"""诊断 Production 组件归属与「谁在用谁」（转换 BP / JavaGateway 派发关系）。

回答的问题：
  1. 当前 Production 里每个业务主机的 `Category`（管道类别）与启用状态；
     （「一条数据管道一个转换 BP」后：`TransformProcess__<类别>` 属该管道，`shared` 只剩 JavaGateway）
  2. 每个源 BS 的 `TargetConfigNames`（派发目标）→ 谁在给哪个转换 BP 发消息；
  3. 哪些组件设置了 `JGService` → 谁在依赖 JavaGateway；
  4. 配置表 `^demo.Config("pipe", <源BS>)` / `^demo.Config("bp", <BP名>)` 内容
     （BP 自身参数为权威，pipe 表仅历史兼容）。

用法（容器内，推荐走 datakit 入口）：
    cd tools/datakit && ./run.sh diag_shared_components.py
或手动：
    docker cp tools/diag_shared_components.py dataflow-backend:/tmp/ \
      && docker exec -w /app dataflow-backend python /tmp/diag_shared_components.py
"""
import json

from backend.services import iris_connector as ic

PROD = "demo.DataflowProduction"


def prod_items() -> list[dict]:
    """Production 组件清单（走 demo.PipelineQuery.GetItems，事实来源）。"""
    raw = ic.class_method_value("demo.PipelineQuery", "GetItems", PROD)
    return json.loads(raw or "[]")


def item_settings() -> dict[str, dict]:
    """Name → {setting: value}（走 demo.PipelineQuery.GetItemsDetail：Host/Adapter 设置都不丢）。"""
    raw = ic.class_method_value("demo.PipelineQuery", "GetItemsDetail", PROD)
    items = json.loads(raw or "[]")
    return {str(i["name"]): dict(i.get("settings") or {}) for i in items}


def pipe_routes() -> dict:
    """^demo.Config("pipe", <源BS>) 路由表（按子节点遍历）。"""
    conn = ic.get_connection()
    try:
        import iris  # type: ignore
        n = iris.createIRIS(conn)
        out, sub = {}, n.nextSubscript(False, "^demo.Config", "pipe", "")
        while sub:
            out[sub] = n.get("^demo.Config", "pipe", sub)
            sub = n.nextSubscript(False, "^demo.Config", "pipe", sub)
        return out
    finally:
        conn.close()


def config_subs(sub1: str) -> dict:
    """读 ^demo.Config(<sub1>, <sub2>) 一层子表（如 "bp" → {BP名: 参数 JSON}）。"""
    conn = ic.get_connection()
    try:
        import iris  # type: ignore
        n = iris.createIRIS(conn)
        out, sub = {}, n.nextSubscript(False, "^demo.Config", sub1, "")
        while sub:
            out[sub] = n.get("^demo.Config", sub1, sub)
            sub = n.nextSubscript(False, "^demo.Config", sub1, sub)
        return out
    finally:
        conn.close()


def topology_dump() -> dict:
    """^demo.Config("pipeline","topology")：本次 Production 的渲染输入（组件构成的事实）。"""
    conn = ic.get_connection()
    try:
        import iris  # type: ignore
        n = iris.createIRIS(conn)
        raw = n.get("^demo.Config", "pipeline", "topology")
        return json.loads(raw) if raw else {}
    finally:
        conn.close()


def main() -> None:
    items = prod_items()
    settings = item_settings()
    routes = pipe_routes()
    bp_cfg = config_subs("bp")            # ^demo.Config("bp", <BP名>) = 本 BP 的转换参数
    bps_target = config_subs("bp_target")  # ^demo.Config("bp_target", <源BS>) = BP 名
    by_name = {str(i["name"]): i for i in items}

    print(f"== Production {PROD} 组件（{len(items)} 个）==")
    for it in items:
        print(f"  [{it.get('category') or '-'}] {it['name']:<34} "
              f"enabled={it.get('enabled')}  {it.get('className')}")

    print("\n== 谁在给转换 BP 派发消息（TargetConfigNames）==")
    print("  （BP 名形如 TransformProcess（单管道）/ TransformProcess__<管道类别>（多管道））")
    users = [n for n, st in settings.items() if "TransformProcess" in st.get("TargetConfigNames", "")]
    print("  使用者:", users or "【无】—— 当前没有源 BS 指向转换 BP")
    bps = [it["name"] for it in items if "TransformProcess" in str(it.get("className") or "")]
    print("  转换 BP 实例:", bps or "【无】",
          "→" if bps else "（本次拓扑无组走通用路由）")

    print("\n== 谁依赖 JavaGateway（JGService 设置）==")
    jg = [n for n, st in settings.items() if st.get("JGService")]
    for n in jg:
        print(f"  {n} -> {settings[n].get('JGService')}")
    print(f"  共 {len(jg)} 个组件依赖 JavaGateway")

    print('\n== 投递/参数配置 ^demo.Config("bp_target", <源BS>) 与 ^demo.Config("bp", <BP名>) ==')
    for bs in sorted(by_name):
        cls = str((by_name.get(bs) or {}).get("className") or "")
        if "JavaGateway" in cls:
            continue
        route = bps_target.get(bs)
        if route:
            print(f"  {bs} -> BP: {route}")
    for bp_name in bps:
        print(f"  bp[{bp_name}] = {bp_cfg.get(bp_name, '（未写：BP 会回退 pipe/全局键）')}")
    if not bps_target and not bp_cfg:
        print("  （空：尚无管道走通用路由，或为历史管道仅写了 pipe 表）")

    print('\n== 兼容路由表 ^demo.Config("pipe", <源BS>) ==')
    for bs, route in routes.items():
        exists = "存在" if bs in by_name else "【组件已不在 Production】"
        cat = (by_name.get(bs) or {}).get("category") or "-"
        print(f"  {bs} (类别={cat}, {exists}) -> {route}")
    if not routes:
        print("  （空：正常——一管道一 BP 后以 bp/bp_target 为准，pipe 表仅历史兼容）")

    topo = topology_dump()
    print(f"\n== ^demo.Config(\"pipeline\",\"topology\") 渲染输入（design_skill="
          f"{topo.get('design_skill')}，{len(topo.get('components') or [])} 组件）==")
    for c in topo.get("components") or []:
        print(f"  {str(c.get('type')):<16} {str(c.get('name')):<34} category={c.get('category')}")


if __name__ == "__main__":
    main()
