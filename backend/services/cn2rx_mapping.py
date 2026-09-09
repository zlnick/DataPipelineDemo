"""C3 术语映射判定 Agent：中文药品名 → RxNorm（多路召回 + LLM 判定）。

召回（确定性，非决策）：
  ① chinese-map 词表直查 → 确定性锚点（中文名 ↔ EnName/RXCUI）
  ② 英文名向量召回 → RxNorm IN/SCD/SBD/BN 多 TTY 候选（英英 1.0 强）
  ③ 中文原词向量召回 → 词表未覆盖时的兜底候选
判定（AI 决策，LLM）：在候选证据上选择最佳 RXCUI（含 TTY 层）或判无映射。
规则/词表/向量不改变判定语义；LLM 失败或召回为空均显式返回（不静默）。
"""
import json
import logging
import os
import urllib.parse
import urllib.request

from backend.services.llm_client import AgentError, _call_llm

logger = logging.getLogger(__name__)

# 术语服务器 Web 基础地址（backend 与 iris-terminology 同 dataflow-net，容器内用服务名 52773）
TERMSRV_BASE = os.environ.get("TERMSRV_BASE", "http://iris-terminology:52773/terminology")
RX_URI = "http://www.nlm.nih.gov/research/umls/rxnorm"

SYSTEM_PROMPT_MAPPING = (
    "你是医疗术语映射判定专家（Mapping Agent）。给定一个中国药品名称（可能含剂型/来源语境）与候选术语列表，"
    "判定其在 RxNorm（美国标准药品术语）中的最佳映射。"
    "规则：1. 优先选择语义精确对应的成分或药品：查询为纯成分/通用名→优先 IN；含剂型且候选有匹配剂型的"
    "SCD/SBD→优先剂型行；查询为明确商品名→BN/SBD。"
    "2. 候选来自多路召回：alias=中文词表确定性命中（最可信），en/ch=向量相似（分数仅供参考）。"
    "若 alias 命中存在，通常以其为准并核对。"
    "3. 警惕同族混淆：如中文『阿托伐他汀』不能被 pravastatin（同为他汀）冒充；『硝苯地平』不能被 amlodipine"
    "（同为二氢吡啶）冒充。候选仅含同族而无比对词干一致的精确成分时，判 match=false。"
    "4. 无任何可靠对应（如中国创新药未收录）时返回 match=false 并说明。"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"match":true,"rxcui":"6809","tty":"IN","enName":"metformin",'
    '"reason":"中文名与 RxNorm 成分 metformin 精确对应",'
    '"confidence":0.97,"alias_based":true}'
)


def _http_get(path: str) -> dict:
    url = TERMSRV_BASE + path
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # 术语服务不可达视为 Agent 基础设施失败，显式上抛
        raise AgentError(f"MappingAgent 召回失败（术语服务 {url}）: {exc}") from exc


def _to_score(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _vector_items(q: str, limit: int) -> list[dict]:
    """向量召回 RxNorm 候选。"""
    path = "/vector/search?q=%s&limit=%d&systemUri=%s" % (
        urllib.parse.quote(q), limit, urllib.parse.quote(RX_URI))
    data = _http_get(path)
    items = []
    for it in data.get("items", []):
        items.append({
            "code": str(it.get("code", "")),
            "text": str(it.get("text", "")),
            "score": _to_score(it.get("score")),
        })
    return [i for i in items if i["code"] and i["score"] > 0]


def recall_candidates(query: str) -> dict:
    """多路召回并合并（按 RXCUI 去重，记录每项证据来源与分数）。"""
    merged: dict[str, dict] = {}
    alias = None

    # ① chinese-map 词表确定性命中
    cm = _http_get("/rxnorm/chinese-map?q=" + urllib.parse.quote(query))
    for it in cm.get("items", []):
        rxcui = str(it.get("rxcui") or "")
        en = str(it.get("enName") or "")
        if rxcui and en:
            alias = {"zhName": it.get("zhName"), "rxcui": rxcui,
                     "enName": en, "tty": it.get("tty", "")}
            merged[rxcui] = {"code": rxcui, "text": en, "score": 1.0, "route": "alias",
                             "tty": it.get("tty", "")}

    # ② 英文名二次召回（词表锚点扩展出 IN/SCD/SBD/BN 全层级候选）
    if alias:
        for it in _vector_items(alias["enName"], 6):
            c = it["code"]
            if c not in merged or it["score"] > merged[c]["score"]:
                merged[c] = {"code": c, "text": it["text"], "score": it["score"],
                             "route": "en", "tty": ""}

    # ③ 中文原词向量兜底召回
    for it in _vector_items(query, 5):
        c = it["code"]
        if c not in merged or it["score"] > merged[c]["score"]:
            merged[c] = {"code": c, "text": it["text"], "score": it["score"],
                         "route": "zh", "tty": ""}

    candidates = sorted(merged.values(), key=lambda x: x["score"], reverse=True)
    return {"alias": alias, "candidates": candidates}


def run_cn2rx_mapping(query: str, context: str | None = None) -> dict:
    """中文药品名 → RxNorm 映射判定。返回判定结果与召回证据。"""
    if not query or not query.strip():
        raise AgentError("MappingAgent 参数缺失：query（中文药名）必填")

    recall = recall_candidates(query.strip())
    candidates = recall["candidates"]
    if not candidates:
        # 无任何候选 = 检索层事实（无证据），不调 LLM；显式标注 recall_empty，不作 AI 判定冒充
        return {
            "match": False, "rxcui": None, "tty": None, "enName": None,
            "reason": "召回无候选：词表与向量均未命中（RxNorm 可能无对应或拼写差异）",
            "confidence": 0.0, "alias_based": False, "recall_empty": True,
            "evidence": {"alias": recall["alias"], "candidates": []},
        }

    user = {
        "中文药名": query,
        "语境/来源": context or "（未提供）",
        "词表确定性命中": recall["alias"] or "无",
        "候选术语": [
            {"rxcui": c["code"], "名称": c["text"], "score": round(c["score"], 3),
             "来源": c["route"]}
            for c in candidates[:12]
        ],
    }
    verdict = _call_llm(SYSTEM_PROMPT_MAPPING, json.dumps(user, ensure_ascii=False),
                        "MappingAgent")

    return {
        "match": bool(verdict.get("match")),
        "rxcui": verdict.get("rxcui"),
        "tty": verdict.get("tty"),
        "enName": verdict.get("enName"),
        "reason": verdict.get("reason", ""),
        "confidence": float(verdict.get("confidence", 0) or 0),
        "alias_based": bool(verdict.get("alias_based")),
        "recall_empty": False,
        "evidence": {"alias": recall["alias"],
                     "candidates": [{**c, "score": round(c["score"], 3)} for c in candidates[:12]]},
    }
