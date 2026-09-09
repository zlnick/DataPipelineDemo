"""C3-Dx 术语映射判定 Agent：中文诊断（国标 ICD-10）→ SNOMED CT（US Core 条件池）。

与药品版 C3（cn2rx_mapping）同架构：
  ① zh-map 双语词表先决（人工核对常见诊断 → 稳定精确）
  ② 命中英文名二次向量召回（SNOMED 池同类/细分扩展）
  ③ 中文原词向量兜底（词表外）
判定（AI 决策）：LLM 在候选证据上选最佳 SNOMED 码或判无映射；词表/向量仅召回，不改变判定。
"""
import json
import logging
import os
import urllib.parse
import urllib.request

from backend.services.llm_client import AgentError, _call_llm

logger = logging.getLogger(__name__)

TERMSRV_BASE = os.environ.get("TERMSRV_BASE", "http://iris-terminology:52773/terminology")
SNOMED_URI = "urn:uscore:condition-sample"

SYSTEM_PROMPT_DX_MAPPING = (
    "你是医疗术语映射判定专家（Diagnosis Mapping Agent）。给定一个中文诊断（国标 ICD-10 名称）与候选英文术语列表，"
    "判定其在 SNOMED CT（US Core Condition Codes 池）中的最佳映射。"
    "规则：1. 候选来源 route：alias=中文诊断词表确定性命中（最可信），en/zh=语义向量相似（分数参考）。"
    "若 alias 命中存在，通常以其为准。"
    "2. 警惕上位/同类混淆：如中文『急性上颌窦炎』不能被 Acute pansinusitis（全窦炎）冒充，"
    "『会厌结核』不能被 Pulmonary tuberculosis（肺结核）冒充；候选仅含上位/病种族而无词干一致的精确项时，判 match=false 并说明。"
    "3. 无任何可靠对应返回 match=false（该诊断可能不在池或需人工补充词表）。"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"match":true,"code":"59621000","display":"Essential hypertension",'
    '"reason":"中文名与 SNOMED Essential hypertension 词表确定命中",'
    '"confidence":0.98,"alias_based":true}'
)


def _http_get(path: str) -> dict:
    url = TERMSRV_BASE + path
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise AgentError(f"MappingAgent 召回失败（术语服务 {url}）: {exc}") from exc


def _score(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _vector_items(q: str, limit: int) -> list[dict]:
    path = "/vector/search?q=%s&limit=%d&systemUri=%s" % (
        urllib.parse.quote(q), limit, urllib.parse.quote(SNOMED_URI))
    items = []
    for it in _http_get(path).get("items", []):
        s = _score(it.get("score"))
        if it.get("code") and s > 0:
            items.append({"code": str(it["code"]), "text": str(it.get("text", "")), "score": s})
    return items


def recall(query: str) -> dict:
    merged: dict[str, dict] = {}
    alias = None
    # ① 词表先决
    cm = _http_get("/uscore-condition/zh-map?q=" + urllib.parse.quote(query))
    for it in cm.get("items", []):
        code = str(it.get("code") or "")
        if code:
            alias = {"zhName": it.get("zhName"), "cnCode": it.get("cnCode"),
                     "code": code, "display": it.get("display", "")}
            merged[code] = {"code": code, "text": it.get("display", ""), "score": 1.0,
                            "route": "alias"}
    # ② alias 英文名二次召回（同类/细分扩展）
    if alias:
        for it in _vector_items(alias["display"], 6):
            if it["code"] not in merged or it["score"] > merged[it["code"]]["score"]:
                merged[it["code"]] = {"code": it["code"], "text": it["text"],
                                      "score": it["score"], "route": "en"}
    # ③ 中文原词兜底
    for it in _vector_items(query, 6):
        if it["code"] not in merged or it["score"] > merged[it["code"]]["score"]:
            merged[it["code"]] = {"code": it["code"], "text": it["text"],
                                  "score": it["score"], "route": "zh"}
    cands = sorted(merged.values(), key=lambda x: x["score"], reverse=True)
    return {"alias": alias, "candidates": cands}


def run_cn2snomed(query: str, context: str | None = None) -> dict:
    if not query or not query.strip():
        raise AgentError("MappingAgent 参数缺失：query（中文诊断名）必填")

    recall_data = recall(query.strip())
    cands = recall_data["candidates"]
    if not cands:
        return {
            "match": False, "code": None, "display": None,
            "reason": "召回无候选：词表与向量均未命中（该诊断可能不在池或词形差异）",
            "confidence": 0.0, "alias_based": False, "recall_empty": True,
            "evidence": {"alias": recall_data["alias"], "candidates": []},
        }

    user = {
        "中文诊断": query.strip(),
        "语境": context or "（未提供）",
        "词表确定性命中": recall_data["alias"] or "无",
        "候选术语": [{"code": c["code"], "display": c["text"], "score": round(c["score"], 3),
                     "route": c["route"]} for c in cands[:12]],
    }
    verdict = _call_llm(SYSTEM_PROMPT_DX_MAPPING,
                        json.dumps(user, ensure_ascii=False), "MappingAgent-Dx")
    return {
        "match": bool(verdict.get("match")),
        "code": verdict.get("code"),
        "display": verdict.get("display"),
        "reason": verdict.get("reason", ""),
        "confidence": float(verdict.get("confidence", 0) or 0),
        "alias_based": bool(verdict.get("alias_based")),
        "recall_empty": False,
        "evidence": {"alias": recall_data["alias"],
                     "candidates": [{**c, "score": round(c["score"], 3)} for c in cands[:12]]},
    }
