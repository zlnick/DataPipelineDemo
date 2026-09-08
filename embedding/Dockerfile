# 本地 Embedding 服务（CPU）——Qwen3-Embedding-0.6B via sentence-transformers
# 构建只装依赖；模型文件运行时由 entrypoint 用 modelscope 下载到 /models 卷
FROM python:3.11-slim

RUN pip install --no-cache-dir \
        torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir \
        sentence-transformers \
        fastapi \
        uvicorn \
        modelscope

WORKDIR /app
COPY embedding_server.py /app/embedding_server.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# 模型缓存目录（模型文件下载到 /models 卷，避免进镜像）
ENV MODEL_DIR=/models/Qwen3-Embedding-0.6B
ENV MODEL_SCOPE_ID=Qwen/Qwen3-Embedding-0.6B
VOLUME /models
EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
