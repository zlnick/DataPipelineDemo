# -*- coding: utf-8 -*-
"""诊断 ^demo.Config 配置树（容器内执行）。

为什么需要它：转换 BP 的转换参数以 **`^demo.Config("bp", <BP名>)`** 为权威
（一条数据管道一个 BP 实例，BP 内用 `..%ConfigName` 读自己那份）；
源 BS → BP 的投递关系写在 `^demo.Config("bp_target", <源BS名>)`；
历史管道可能只写了 `^demo.Config("pipe", <源BS名>)` 与全局单值键（兜底链）。
本脚本把整棵 `^demo.Config` 打出来，用于判断「BP 读的是哪一份参数 / 有没有漏写」。

用法（容器内，推荐走 datakit 入口）：
    cd tools/datakit && ./run.sh diag_demo_config.py
或手动：
    docker cp tools/diag_demo_config.py dataflow-backend:/tmp/ \
      && docker exec -w /app dataflow-backend python /tmp/diag_demo_config.py
"""
import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("diag_demo_config")


def _child_ref(node, sub):
    """按 IRIS global 引用规则拼下一级下标：`^demo.Config` → `^demo.Config("a")`；
    `^demo.Config("a")` → `^demo.Config("a","b")`（不能写成 (...)(...)）。"""
    s = str(sub)
    key = s if s.isdigit() else f'"{s}"'
    return f"{node[:-1]},{key})" if node.endswith(")") else f"{node}({key})"


def _dump(native, prefix, node, depth=0, max_depth=3):
    """递归打印 global 子树（限制深度，避免拓扑 JSON 刷屏）。"""
    try:
        sub = native.nextSubscript(False, node, "")
    except Exception as exc:  # noqa: BLE001
        log.warning("  枚举 %s 失败: %s", node, str(exc)[:100])
        return
    if not sub:
        try:
            value = native.get(node)
        except Exception:  # noqa: BLE001
            value = None
        text = "" if value is None else str(value)
        log.info("%s%s = %s", "  " * depth, prefix, text[:200] + ("…" if len(text) > 200 else ""))
        return
    while sub:
        child = _child_ref(node, sub)
        if depth < max_depth:
            _dump(native, f"{prefix}/{sub}", child, depth + 1, max_depth)
        else:
            log.info("%s%s/%s …（更深，略）", "  " * depth, prefix, sub)
        try:
            sub = native.nextSubscript(False, node, sub)
        except Exception:  # noqa: BLE001
            break


def main():
    from backend.services import iris_connector
    import iris
    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        # 顶层键
        tops = []
        sub = native.nextSubscript(False, "^demo.Config", "")
        while sub:
            tops.append(str(sub))
            sub = native.nextSubscript(False, "^demo.Config", sub)
        log.info("== ^demo.Config 一级下标 ==\n  %s", tops or ["（空）"])

        for key in ("bp", "bp_target", "pipe", "pipeline", "soap", "fhir", "sql2fhir"):
            node = f'^demo.Config("{key}")'
            log.info("\n== %s ==", node)
            try:
                has = native.nextSubscript(False, node, "")
            except Exception as exc:  # noqa: BLE001
                log.info("  枚举失败: %s", str(exc)[:120])
                continue
            if not has:
                log.info("  （空）")
                continue
            _dump(native, key, node, 1, max_depth=2)

        # 关键结论（一管道一 BP 后的判读）
        log.info("\n== 判读 ==")
        bp_subs = []
        s = native.nextSubscript(False, '^demo.Config("bp")', "")
        while s:
            bp_subs.append(str(s))
            s = native.nextSubscript(False, '^demo.Config("bp")', s)
        t_subs = []
        s = native.nextSubscript(False, '^demo.Config("bp_target")', "")
        while s:
            t_subs.append(str(s))
            s = native.nextSubscript(False, '^demo.Config("bp_target")', s)
        if bp_subs:
            log.info("  转换 BP 自身参数: %s", bp_subs)
            log.info("  源 BS → BP 投递表: %s",
                     {k: native.get("^demo.Config", "bp_target", k) for k in t_subs})
            log.info("  → 正常：每条管道的 BP 读自己那份 bp[<BP名>]（一管道一 BP，互不干扰）。")
        elif t_subs:
            log.info("  仅写了 bp_target（BP 参数缺失）→ BP 会回退 pipe/全局键，请检查生成流程。")
        else:
            log.info('  无 bp/bp_target 配置 → 属历史管道或尚未生成；')
            log.info('  BP 将退回 ^demo.Config("pipe", <源BS>) 或全局键 '
                     '(pipeline.active_mapping / target_type / soap.active_service)。')
    finally:
        conn.close()


if __name__ == "__main__":
    main()
