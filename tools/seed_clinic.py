# -*- coding: utf-8 -*-
"""为 CLINIC SQL 源生成演示数据（等价界面「生成演示数据」按钮）。"""
import json
import urllib.request

BASE = "http://localhost:5001/api"
items = json.load(urllib.request.urlopen(BASE + "/datasources"))["data"]["items"]
ds = [d for d in items if d.get("type") == "SQL"][0]
print("datasource:", ds["id"], ds.get("name"))
req = urllib.request.Request(
    f"{BASE}/datasources/{ds['id']}/seed",
    data=json.dumps({"patients": 3}).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
print(urllib.request.urlopen(req, timeout=900).read().decode()[:300])
