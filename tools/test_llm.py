# -*- coding: utf-8 -*-
"""LLM 连通性自检（在 dataflow-backend 容器内运行）。

覆盖四层，逐层定位问题：
  ① 配置读取：容器生效的 env / .env 待生效值（并列出两者差异）
  ② 端点与密钥：GET {base_url}/models → 该 key 真正可用的模型清单
  ③ 模型名与参数：对候选模型逐个做最小 chat 探测（max_tokens=64），
     再用平台默认 LLM_MAX_TOKENS 探测一次（判断平台默认值是否被服务商接受）
  ④ 平台真实链路：直接调用 backend.services.llm_client._call_llm（含 response_format=json_object）

用法（本机仓库根）：
    docker cp tools/test_llm.py dataflow-backend:/tmp/test_llm.py
    docker cp .env dataflow-backend:/tmp/pending.env
    docker exec dataflow-backend python /tmp/test_llm.py
"""
from __future__ import annotations

import os
import sys

# 允许两种运行方式：容器内 /tmp 直跑（自动补 repo 路径）或仓库内 python -m
for _p in ("/app", os.getcwd(), os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

from openai import OpenAI  # noqa: E402

PENDING_ENV = "/tmp/pending.env"
PROBE_MAX_TOKENS = 64


def mask(key: str) -> str:
    """密钥脱敏显示。"""
    key = key or ""
    if len(key) <= 8:
        return "***"
    return f"{key[:6]}...{key[-4:]}(len={len(key)})"


def parse_env_file(path: str) -> dict:
    """读取 .env（简单解析，不依赖 python-dotenv）。"""
    out: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def show_config() -> dict:
    """打印并返回「容器生效」与「.env 待生效」两套配置。"""
    live = {
        "base": os.getenv("LLM_BASE_URL", ""),
        "key": os.getenv("LLM_API_KEY", ""),
        "model": os.getenv("LLM_MODEL", ""),
        "max_tokens": os.getenv("LLM_MAX_TOKENS", ""),
    }
    pend = parse_env_file(PENDING_ENV)
    print("=" * 72)
    print("① 配置读取")
    print(f"  容器生效 : base={live['base']}  model={live['model']}  "
          f"max_tokens={live['max_tokens'] or '(未设置→平台默认)'}  key={mask(live['key'])}")
    if pend:
        print(f"  .env 待生效: base={pend.get('LLM_BASE_URL', '')}  "
              f"model={pend.get('LLM_MODEL', '')}  key={mask(pend.get('LLM_API_KEY', ''))}")
        diff = [k for k, v in (("LLM_BASE_URL", "base"), ("LLM_MODEL", "model"),
                               ("LLM_API_KEY", "key")) if pend.get(k, "") != live[v]]
        if diff:
            print(f"  ⚠ 两者不一致: {', '.join(diff)} → 需 `docker compose restart backend` 才生效")
        else:
            print("  ✓ 两者一致")
    else:
        print("  (.env 待生效值未提供；如需对比请 docker cp .env dataflow-backend:/tmp/pending.env)")
    return live


def list_models(base: str, key: str) -> list:
    """② 拉取可用模型清单（端点+密钥是否有效的最直接证据）。"""
    print("=" * 72)
    print("② 端点与密钥：GET /models")
    try:
        client = OpenAI(base_url=base, api_key=key, timeout=30.0)
        ids = sorted(m.id for m in client.models.list().data)
        print(f"  ✓ 端点可达、密钥有效，可用模型 {len(ids)} 个:")
        for mid in ids:
            print(f"      - {mid}")
        return ids
    except Exception as exc:  # noqa: BLE001 - 自检脚本需回显任何异常
        print(f"  ✗ 端点或密钥异常: {type(exc).__name__}: {str(exc)[:300]}")
        return []


def probe_chat(base: str, key: str, model: str, max_tokens: int,
               json_mode: bool = False) -> tuple:
    """③ 最小 chat 探测；返回 (是否成功, 说明)。"""
    try:
        client = OpenAI(base_url=base, api_key=key, timeout=60.0)
        kwargs = {
            "model": model,
            "messages": [{"role": "user", "content": "只回复 json: {\"ok\":1}"}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(**kwargs)
        usage = getattr(resp, "usage", None)
        text = (resp.choices[0].message.content or "").strip().replace("\n", " ")[:60]
        used = f"token={usage.total_tokens}" if usage else "token=?"
        return True, f"ok（{used}）out={text!r}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:240]}"


def run() -> int:
    """执行四层自检并给出结论（返回进程退出码）。"""
    live = show_config()
    base = live["base"] or "https://api.deepseek.com"
    key = live["key"]
    if not key:
        print("✗ 未配置 LLM_API_KEY，终止")
        return 1

    ids = list_models(base, key)

    # 候选模型 = 容器生效值 + .env 待生效值 + /models 返回的前 6 个
    pend = parse_env_file(PENDING_ENV)
    candidates: list = []
    for m in (live["model"], pend.get("LLM_MODEL", "")):
        if m and m not in candidates:
            candidates.append(m)
    for mid in ids[:6]:
        if mid not in candidates:
            candidates.append(mid)

    print("=" * 72)
    print("③ 模型名 + 参数探测（max_tokens=64，json_object 模式）")
    ok_models: list = []
    for model in candidates:
        ok, note = probe_chat(base, key, model, PROBE_MAX_TOKENS, json_mode=True)
        print(f"  {'✓' if ok else '✗'} {model}: {note}")
        if ok:
            ok_models.append(model)

    print("=" * 72)
    print("③b 平台默认 max_tokens 兼容性（LLM_MAX_TOKENS，平台默认 64000）")
    for model in ok_models[:2]:
        ok, note = probe_chat(base, key, model, 64000, json_mode=True)
        print(f"  {'✓' if ok else '✗'} {model} @max_tokens=64000: "
              f"{'ok（服务商接受该值）' if ok else note}")

    print("=" * 72)
    print("④ 平台真实链路：backend.services.llm_client._call_llm（Agent A/B 同款）")
    target_model = pend.get("LLM_MODEL") or live["model"]
    llm_ok = False
    try:
        from backend.config import LLMConfig
        from backend.services import llm_client

        # 按 LLM_MAX_TOKENS 环境变量生效（未设置时用 8192，避免服务商上限拒绝）
        LLMConfig.MODEL = target_model
        LLMConfig.MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "8192"))
        llm_client.LLMConfig.MODEL = target_model
        llm_client.LLMConfig.MAX_TOKENS = LLMConfig.MAX_TOKENS
        result = llm_client._call_llm(
            "你是数据集成助手，只输出 json 对象。",
            "输出 {\"pong\":true}",
            "自检Agent",
        )
        print(f"  ✓ _call_llm 通过（model={target_model}，JSON 解析成功）: {result}")
        llm_ok = True
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ _call_llm 失败（model={target_model}）: {type(exc).__name__}: {str(exc)[:400]}")

    print("=" * 72)
    print("结论")
    if llm_ok:
        print(f"  ✓ LLM 可调通（model={target_model}）")
        if pend.get("LLM_MODEL") != live["model"]:
            print("  ⚠ 容器内生效值仍与 .env 不一致 → 执行 `docker compose restart backend` 使新配置生效")
        return 0
    if ok_models:
        print(f"  ⚠ 服务商实际可用模型: {ok_models[:5]}；配置的 '{target_model}' 不可用 → 修正 .env 的 LLM_MODEL")
        return 2
    print("  ✗ 该 base_url + key 下无任何模型可用 → 检查 BASE_URL / API_KEY（端点、密钥、网络）")
    return 1


if __name__ == "__main__":
    raise SystemExit(run())
