"""本地 Embedding 服务（OpenAI 兼容 /v1/embeddings）。

模型：Qwen3-Embedding-0.6B（多语言 100+，1024 维），模型文件由 entrypoint
从 ModelScope 下载到卷 /models（国内可直连），sentence-transformers 加载。
"""
import os
import threading
from typing import List, Union

import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel

MODEL_DIR = os.environ.get("MODEL_DIR", "/models/Qwen3-Embedding-0.6B")

app = FastAPI(title="Embedding Service", version="1.0.0")

_model = None
_lock = threading.Lock()


def get_model():
    """惰性加载 SentenceTransformer（首次调用慢，加载后复用）。"""
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer(MODEL_DIR)
    return _model


class EmbeddingRequest(BaseModel):
    model: str = "Qwen3-Embedding-0.6B"
    input: Union[str, List[str]]
    # encoding_format 仅作兼容字段（我们总是返回 float 数组）
    encoding_format: str = "float"


class EmbeddingData(BaseModel):
    object: str = "embedding"
    index: int = 0
    embedding: List[float] = []


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: List[EmbeddingData] = []
    model: str = ""
    usage: dict = {}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": os.path.basename(MODEL_DIR)}


@app.post("/v1/embeddings", response_model=EmbeddingResponse)
def embed(req: EmbeddingRequest) -> EmbeddingResponse:
    texts = [req.input] if isinstance(req.input, str) else list(req.input)
    if not texts:
        raise ValueError("input 不能为空")
    model = get_model()
    vectors = model.encode(texts, normalize_embeddings=True)
    vectors = np.asarray(vectors, dtype="float32")
    data = [
        EmbeddingData(index=i, embedding=[float(x) for x in vectors[i]])
        for i in range(len(texts))
    ]
    return EmbeddingResponse(
        data=data,
        model=req.model,
        usage={
            "prompt_tokens": 0,
            "total_tokens": 0,
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
