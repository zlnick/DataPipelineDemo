"""Embedding 服务预热（中英混合）：容器重启后首次中文推理可能不稳定，先跑本脚本暖机。"""
import json
import urllib.request

URL = "http://127.0.0.1:8001/v1/embeddings"
TEXTS = ["metformin", "阿基仑赛注射液", "二甲双胍", "insulin glargine", "布洛芬"]

data = json.dumps({"input": TEXTS}, ensure_ascii=False).encode("utf-8")
req = urllib.request.Request(URL, data=data, headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=180) as resp:
    out = json.loads(resp.read().decode("utf-8"))
print("warm ok:", len(out["data"]), "embeddings")
