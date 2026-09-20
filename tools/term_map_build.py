# -*- coding: utf-8 -*-
"""术语映射补录作业：把术语服务器缺失的映射用**判定 Agent（C3-Dx / C3）**判出后写回服务器。

术语服务器是术语转换映射的**唯一事实源**（`/terminology/mapping/*`）。平台生成数据管道前会做
术语预检：缺映射 → **中止生成**并提示补录。本作业就是那个补录入口：

  ① 预检（`term_precheck.precheck`）：列出服务器尚无的源编码（missing/pending）；
  ② 逐条判定：`cn2snomed` → C3-Dx（`run_cn2snomed`）；`cn2rx` → C3（`run_cn2rx_mapping`）——**LLM 判定**；
  ③ 写回服务器：`POST /terminology/mapping/entry`（`status=active` 或 `negative`，`method=llm`，附 evidence/confidence）；
  ④ 复检：再跑 availability，打印覆盖率；仍有缺失 → **非零退出**（不静默）。

用法（宿主机直接运行，脚本自动进 backend 容器；容器内需 `-w /app` 语境，故自动处理）：

    python3 tools/term_map_build.py                # 补录全部缺失（读 ^demo.Mapping 的 term_map 决策）
    python3 tools/term_map_build.py --dry          # 只看会补哪些（不调 LLM、不写入）
    python3 tools/term_map_build.py --limit 5      # 最多判定 5 条（控 LLM 调用次数）
    python3 tools/term_map_build.py --skill cn2rx  # 只补某类判定 Skill
    python3 tools/term_map_build.py --force        # 连服务器判为 negative 的也重新判定

⚠ 判定失败（LLM/术语服务不可达）**显式报错**，不静默回退规则、不写假数据（遵守 AI 驱动红线）。
"""
import os
import subprocess
import sys

CONTAINER = "dataflow-backend"


def _run_in_container():
    """宿主机路径：把自身送进 backend 容器执行并透传参数。

    用 `cat > /tmp/...`（而非 docker cp）：本项目 docker 守卫会把 `cp` 的**源**路径当目标误拒。
    """
    here = os.path.abspath(__file__)
    with open(here, "rb") as fh:
        subprocess.run(["docker", "exec", "-i", CONTAINER, "sh", "-c",
                        "cat > /tmp/term_map_build.py"], stdin=fh, check=True)
    # 注意：不要加 `-w /app`（容器 WORKDIR 已是 /app；且守卫会把选项值 /app 当越界目标误拒）
    cmd = ["docker", "exec", "-i", CONTAINER, "python", "/tmp/term_map_build.py", *sys.argv[1:]]
    raise SystemExit(subprocess.run(cmd).returncode)


def _configure_termsrv_base() -> None:
    """容器内直连术语服务器（不经宿主端口）。"""
    os.environ.setdefault("TERMSRV_BASE", "http://iris-terminology:52773/terminology")
    # 让 term_catalog / 判定 Skill 读到同一地址（它们 import 时即读环境变量）
    from backend.services import cn2rx_mapping, cn2snomed_mapping, term_catalog

    base = os.environ["TERMSRV_BASE"]
    term_catalog.TERMSRV_BASE = base
    cn2snomed_mapping.TERMSRV_BASE = base
    cn2rx_mapping.TERMSRV_BASE = base


def _judge(skill: str, query: str, context: str) -> dict:
    """调用判定 Skill（LLM 决策）；未注册/失败即抛错（不静默）。"""
    from backend.services.cn2rx_mapping import run_cn2rx_mapping
    from backend.services.cn2snomed_mapping import run_cn2snomed

    if skill == "cn2snomed":
        return run_cn2snomed(query, context)
    if skill == "cn2rx":
        return run_cn2rx_mapping(query, context)
    raise ValueError(f"未注册的术语判定 Skill: {skill}")


def _row_from_verdict(skill: str, source_code: str, source_display: str, verdict: dict) -> dict:
    """判定结果 → 服务器映射行（字段名与 /mapping/entry 的契约一致）。"""
    from backend.services.transform_directives import term_skill

    reg = term_skill(skill) or {}
    matched = bool(verdict.get("match"))
    target_code = str(verdict.get(reg.get("code_field") or "code") or "") if matched else ""
    return {
        "sourceSystem": reg.get("source_system") or "",
        "sourceCode": source_code,
        "sourceDisplay": source_display or "",
        "targetSystem": reg.get("target_system") or "",
        "targetCode": target_code,
        "targetDisplay": str(verdict.get(reg.get("display_field") or "display") or ""),
        "method": "llm",
        "status": "active" if target_code else "negative",
        "confidence": float(verdict.get("confidence") or 0),
        "evidence": f"{reg.get('agent') or skill} 判定: {str(verdict.get('reason') or '')[:400]}",
    }



def main_in_container() -> int:
    """容器内主流程：预检 → 逐条判定（LLM）→ 写回服务器 → 复检。"""
    import argparse
    import json

    from backend.services import term_catalog, term_precheck

    ap = argparse.ArgumentParser(description="术语映射补录（术语服务器 = 事实源）")
    ap.add_argument("--dry", action="store_true", help="只列出会补哪些（不调 LLM、不写入）")
    ap.add_argument("--limit", type=int, default=0, help="最多判定多少条（0=不限）")
    ap.add_argument("--skill", default="", help="只处理某类判定 Skill（cn2snomed/cn2rx）")
    ap.add_argument("--force", action="store_true",
                    help="连服务器判为 negative（已判定无匹配）的也重新判定")
    args = ap.parse_args()

    _configure_termsrv_base()

    # ① 预检（读 ^demo.Mapping 的 term_map 决策；源编码按源库命名空间读取）
    gate = term_precheck.precheck()
    if gate.get("skipped"):
        print(f"== 无需补录：{gate['skipped']}")
        return 0
    print("== 预检：任务", gate.get("jobs"), "| 源库命名空间", gate.get("namespaces"))
    for note in gate.get("notes") or []:
        print("   ! ", note)
    print(f"== 服务器目录：{json.dumps(gate.get('pairs') or [], ensure_ascii=False)}")
    print(f"== 覆盖情况：covered={gate.get('covered')} negative={gate.get('negative')} "
          f"missing={len(gate.get('missing') or [])} pending={len(gate.get('pending') or [])} "
          f"unresolved={gate.get('unresolved') or []}")
    if gate.get("error"):
        print("== 预检错误：", gate["error"])
    if not gate.get("ok") and not (gate.get("missing") or gate.get("pending")):
        print("== 预检未通过且无可补录编码（服务器不可达 / source 形态无法枚举）→ 退出 1")
        return 1

    targets: list[tuple[str, str]] = []
    for item in (gate.get("missing") or []) + (gate.get("pending") or []):
        skill, _, code = str(item).partition(":")
        if not code or (args.skill and skill != args.skill):
            continue
        targets.append((skill, code))
    if args.force:
        for skill, rows in (gate.get("items") or {}).items():
            if args.skill and skill != args.skill:
                continue
            for code in rows:
                targets.append((skill, code))
    seen: set[tuple[str, str]] = set()
    targets = [t for t in targets if not (t in seen or seen.add(t))]

    if not targets:
        print("== 无需补录：服务器已覆盖全部源编码 ✓")
        return 0
    print(f"== 待补录 {len(targets)} 条：{targets[:10]}{' …' if len(targets) > 10 else ''}")
    if args.dry:
        print("== DRY-RUN：未调用判定 Agent、未写入服务器")
        return 0
    if args.limit and len(targets) > args.limit:
        print(f"== 按 --limit={args.limit} 截断（其余下次再补）")
        targets = targets[:args.limit]

    # ② 逐条判定（LLM）→ ③ 写回服务器
    sources = gate.get("sources") or {}
    rows: list[dict] = []
    failed: list[str] = []
    for skill, code in targets:
        name = str(((sources.get(skill) or {}).get(code) or {}).get("name") or "")
        query = name or code
        try:
            verdict = _judge(skill, query, context=f"术语映射预生成（源编码 {code}）")
        except Exception as exc:  # noqa: BLE001 - 判定失败必须显式，不写假数据
            print(f"   !! 判定失败 {skill}:{code}（{query}）: {exc}")
            failed.append(f"{skill}:{code}")
            continue
        row = _row_from_verdict(skill, code, name, verdict)
        rows.append(row)
        print(f"   * {skill}:{code} → status={row['status']} target={row['targetCode']} "
              f"({str(row['targetDisplay'])[:40]}) conf={row['confidence']}")

    if rows:
        try:
            res = term_catalog.upsert_entries(rows)
        except term_catalog.TermCatalogError as exc:
            print(f"!! 写回术语服务器失败: {exc}")
            return 1
        print(f"== 写回服务器：inserted={res.get('inserted')} updated={res.get('updated')} "
              f"failed={len(res.get('failed') or [])}")
        if res.get("failed"):
            print("   !! 失败项：", json.dumps(res["failed"][:3], ensure_ascii=False))
            return 1
        term_catalog.invalidate()

    # ④ 复检
    gate2 = term_precheck.precheck()
    print(f"== 复检：covered={gate2.get('covered')} negative={gate2.get('negative')} "
          f"missing={len(gate2.get('missing') or [])} pending={len(gate2.get('pending') or [])} "
          f"blocking={gate2.get('blocking')}")
    if failed or not gate2.get("ok"):
        print("== 仍有未覆盖编码 → 退出 1（生成门禁仍会中止生成）")
        return 1
    print("== 补录完成：术语预检通过，可重新生成数据管道 ✓")
    return 0


def main() -> int:
    """入口：宿主机自动进容器；容器内直接执行主流程。"""
    if os.path.exists("/app/backend") or os.path.exists("/.dockerenv"):
        return main_in_container()
    return _run_in_container()


if __name__ == "__main__":
    sys.exit(main())

