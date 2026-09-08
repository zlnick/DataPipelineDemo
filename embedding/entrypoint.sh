#!/bin/sh
# 首次启动用 ModelScope 下载 Qwen3-Embedding 模型到卷（国内可直连），随后启动 uvicorn
set -e

echo "[embedding] MODEL_DIR=$MODEL_DIR"
if [ ! -f "$MODEL_DIR/config.json" ]; then
  echo "[embedding] 模型缺失，从 ModelScope 下载 $MODEL_SCOPE_ID ..."
  modelscope download --model "$MODEL_SCOPE_ID" --local_dir "$MODEL_DIR"
  echo "[embedding] 模型下载完成"
else
  echo "[embedding] 模型已存在，跳过下载"
fi

echo "[embedding] 启动 uvicorn ..."
exec python -m uvicorn embedding_server:app --host 0.0.0.0 --port 8000
