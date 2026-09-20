# -*- coding: utf-8 -*-
"""术语服务器「转换映射目录」客户端（平台侧唯一出口）。

架构（用户已确认）：**术语服务器是术语转换的唯一事实源** —— 映射的产出/存储/检索
（`/terminology/mapping/*`）都在服务器侧；平台不复制码表、不新增第二套判定规则。

平台侧用法（生成链路）：
  ① 生成前 `precheck`（本模块 availability）判定"这些源编码能否转换"：
     - covered  → 服务器已有该映射（运行期由**共享 BO** demo.TerminologyOperation 实时查询生效）；
     - negative → 已判定无映射，不阻断、不追加 coding（按服务器事实）；
     - missing  → **中止生成**并显式告知用户去补录（`tools/term_map_build.py`）；
  ② 运行期由共享 BO `demo.TerminologyOperation` 实时调 `/mapping/lookup`（零本地码表副本）。

失败策略：服务器不可达 = **显式异常**（`TermCatalogError`）。调用方（生成门禁）必须在
"确实存在 term_map 决策"时才 fail-closed；没有任何术语映射的管道完全不触发本模块。
"""
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# 与 backend/services/cn2snomed_mapping.py 等既有服务同一口径（容器内地址）
TERMSRV_BASE = os.environ.get("TERMSRV_BASE", "http://iris-terminology:52773/terminology")
TIMEOUT = float(os.environ.get("TERMSRV_TIMEOUT", "20"))
CATALOG_TTL = float(os.environ.get("TERM_CATALOG_TTL", "60"))
CHUNK = int(os.environ.get("TERM_AVAILABILITY_CHUNK", "200"))

_cache: dict = {"at": 0.0, "data": None}


class TermCatalogError(Exception):
    """术语服务器调用失败（不可达 / 非 2xx / 返回体不可解析）。"""


def base_url() -> str:
    """当前术语服务器基地址（供 UI/诊断展示；容器内地址）。"""
    return TERMSRV_BASE.rstrip("/")


def invalidate() -> None:
    """清空目录缓存（补录后强制刷新用）。"""
    _cache["at"] = 0.0
    _cache["data"] = None


def _request(path: str, payload: dict | None = None, *, timeout: float | None = None) -> dict:
    """单次调用术语服务器 REST；返回解析后的 JSON（失败抛 TermCatalogError）。"""
    url = base_url() + path
    data = None
    headers = {"Accept": "application/json"}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        method = "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 - 错误体读取失败不影响报错
            pass
        raise TermCatalogError(f"术语服务器返回 HTTP {exc.code}（{url}）：{body}") from exc
    except Exception as exc:  # noqa: BLE001 - 网络/超时统一显式报错
        raise TermCatalogError(f"术语服务器不可达（{url}）：{exc}") from exc
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise TermCatalogError(f"术语服务器返回体不是 JSON（{url}）：{text[:200]}") from exc
    if isinstance(obj, dict) and obj.get("error"):
        raise TermCatalogError(f"术语服务器报错（{url}）：{obj.get('message')}")
    return obj


def systems(*, use_cache: bool = True, timeout: float | None = None) -> dict:
    """目录：服务器上可用的（源体系 → 目标体系）对（含条目数与状态分布）。

    返回 ``{"ok", "pairs": [...], "error", "url"}``；失败**不抛**（目录只作提示/上下文注入）。
    短 TTL 缓存（默认 60s）避免生成链路里反复拉取。
    """
    now = time.time()
    if use_cache and _cache["data"] is not None and (now - float(_cache["at"])) < CATALOG_TTL:
        return dict(_cache["data"])
    try:
        obj = _request("/mapping/systems", timeout=timeout)
    except TermCatalogError as exc:
        logger.warning("术语目录不可用: %s", exc)
        out = {"ok": False, "pairs": [], "error": str(exc), "url": base_url(),
               "note": "术语服务器不可达时无法确认术语映射覆盖面（生成门禁会显式失败）"}
        return out
    pairs = obj.get("pairs") or []
    out = {"ok": True, "pairs": pairs, "error": "", "url": base_url(),
           "count": int(obj.get("count") or len(pairs))}
    _cache["at"] = now
    _cache["data"] = out
    return dict(out)


def pair_info(skill: str) -> dict:
    """按 Skill 取它对应的（源体系, 目标体系）在服务器目录中的状态。"""
    from backend.services import transform_directives as td

    reg = td.term_skill(skill) or {}
    src = str(reg.get("source_system") or "")
    tgt = str(reg.get("target_system") or "")
    info = {"skill": skill, "sourceSystem": src, "targetSystem": tgt,
            "declared": bool(src and tgt), "available": False, "entries": 0,
            "active": 0, "negative": 0}
    cat = systems()
    for p in cat.get("pairs") or []:
        if (str(p.get("sourceSystem") or "").lower() == src.lower()
                and str(p.get("targetSystem") or "").lower() == tgt.lower()):
            info["available"] = True
            info["entries"] = int(p.get("entries") or 0)
            info["active"] = int(p.get("active") or 0)
            info["negative"] = int(p.get("negative") or 0)
            break
    info["catalogOk"] = bool(cat.get("ok"))
    return info

def availability(source_system: str, target_system: str, codes: list[str], *,
                 chunk: int | None = None, timeout: float | None = None) -> dict:
    """批量可用性预检：这些源编码在该体系对上能否转换（分块调用 + 合并）。

    返回::

        {"ok", "total", "covered", "negative", "missingCount", "pendingCount", "blocking",
         "coveredCodes": [...], "negativeCodes": [...], "missingCodes": [...], "pendingCodes": [...],
         "items": {<请求原样码>: {…映射行…}}, "chunks": n, "sourceSystem", "targetSystem"}

    语义（与服务器一致）：covered=可迁移为本地缓存；negative=已判定无映射（不阻断）；
    ``blocking = missingCount + pendingCount`` —— **>0 表示平台必须中止生成并要求补录**。

    失败抛 `TermCatalogError`（不静默降级：宁可显式失败，也不生成缺目标 coding 的资源）。
    """
    uniq: list[str] = []
    seen: set[str] = set()
    for c in codes or []:
        s = str(c or "").strip()
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    out = {"sourceSystem": source_system, "targetSystem": target_system, "total": len(uniq),
           "ok": True, "covered": 0, "negative": 0, "missingCount": 0, "pendingCount": 0,
           "blocking": 0, "coveredCodes": [], "negativeCodes": [], "missingCodes": [],
           "pendingCodes": [], "items": {}, "chunks": 0}
    if not uniq:
        return out
    size = max(1, int(chunk or CHUNK))
    for i in range(0, len(uniq), size):
        part = uniq[i:i + size]
        obj = _request("/mapping/availability",
                       {"sourceSystem": source_system, "targetSystem": target_system, "codes": part},
                       timeout=timeout)
        out["chunks"] += 1
        out["covered"] += int(obj.get("covered") or 0)
        out["negative"] += int(obj.get("negative") or 0)
        out["missingCount"] += int(obj.get("missingCount") or 0)
        out["pendingCount"] += int(obj.get("pendingCount") or 0)
        for field in ("coveredCodes", "negativeCodes", "missingCodes", "pendingCodes"):
            out[field].extend([str(x) for x in (obj.get(field) or [])])
        for code, row in (obj.get("items") or {}).items():
            out["items"][code] = row
    out["blocking"] = int(out["missingCount"]) + int(out["pendingCount"])
    out["ok"] = out["blocking"] == 0
    return out


def entries(source_system: str = "", target_system: str = "", status: str = "",
            limit: int = 200) -> dict:
    """映射清单（服务器侧）：``{"count", "items": [...]}``。"""
    q = {"limit": str(int(limit or 200))}
    if source_system:
        q["sourceSystem"] = source_system
    if target_system:
        q["targetSystem"] = target_system
    if status:
        q["status"] = status
    return _request("/mapping/entries?" + urllib.parse.urlencode(q))


def coverage(source_system: str, target_system: str = "", status: str = "") -> dict:
    """覆盖率统计：``{"entries", "byStatus": {...}}``（缺必填参数时服务器报 400 → 显式异常）。"""
    q = {"sourceSystem": source_system}
    if target_system:
        q["targetSystem"] = target_system
    if status:
        q["status"] = status
    return _request("/mapping/coverage?" + urllib.parse.urlencode(q))


def upsert_entry(row: dict) -> dict:
    """幂等补录单条映射（method/status/evidence 由调用方决定）。"""
    return _request("/mapping/entry", row)


def upsert_entries(rows: list[dict]) -> dict:
    """批量幂等补录：``{"ok","inserted","updated","failed"}``。"""
    return _request("/mapping/entries", {"items": list(rows or [])})

