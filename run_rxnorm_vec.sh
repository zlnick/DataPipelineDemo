#!/bin/bash
# RxNorm 全量向量化闭环（两阶段防 OOM/过热 + 断点续传自愈）。
# 输出 JSONL 与日志在 data/（防宿主重启丢失），embedding 崩溃自动拉起并从已写行数续跑。
#
# ⚠ 这是**可选的运维脚本**，不属于演示初始化（演示只用仓库里的成品映射 data/seeds/term_map_seed.json）。
#   前置条件：
#     ① docker compose up -d embedding            # 本地向量服务（首次自动下载模型，约 1.1GB）
#     ② bash tools/termsrv_vector_init.sh        # 查看向量能力状态（幂等；表已就绪时仅报告）
#     ③ RxNorm 原始数据需自备（许可约束，未随仓库分发）
#   产物：Terminology_Vector.TermEmbedding —— 供 /terminology/vector/search 与 AI 判码（C3）候选召回使用。
set -u
cd "$(dirname "$0")"
LOG=data/rxnorm_vec_run.log
JSONL=data/rxnorm_vec_SCD_SBD_IN.jsonl
mkdir -p data

echo "[$(date '+%F %T')] === start ===" >> "$LOG"
# 阶段1：IRIS 让位，embedding 独占跑向量化
docker compose stop iris-terminology >> "$LOG" 2>&1 || true
docker compose up -d embedding >> "$LOG" 2>&1 || true
sleep 15
# 预热 embedding（中英混合，防冷启动后首次中文推理不稳定）
.venv/bin/python tools/warm_embed.py >> "$LOG" 2>&1 || true

while :; do
  echo "[$(date '+%F %T')] save-vec (resume)" >> "$LOG"
  .venv/bin/python tools/rxnorm_import.py --save-vec --ttys SCD,SBD,IN --batch 200 --resume >> "$LOG" 2>&1
  rc=$?
  if [ $rc -eq 0 ]; then
    break
  fi
  echo "[$(date '+%F %T')] save-vec failed (rc=$rc), restart embedding and retry" >> "$LOG"
  docker compose up -d embedding >> "$LOG" 2>&1 || true
  sleep 30
done

echo "[$(date '+%F %T')] save done: $(wc -l < "$JSONL") lines, switch to load" >> "$LOG"
# 阶段2：embedding 让位，IRIS 独占灌库
docker compose stop embedding >> "$LOG" 2>&1 || true
docker compose up -d iris-terminology >> "$LOG" 2>&1 || true
sleep 15
.venv/bin/python tools/term_embed.py --load "$JSONL" >> "$LOG" 2>&1
echo "[$(date '+%F %T')] DONE" >> "$LOG"
