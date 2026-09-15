# -*- coding: utf-8 -*-
"""打印 FHIR 患者 name 原始 JSON（判定中文是否正确落地，而非终端编码问题）。"""
import base64
import json
import urllib.request

BASE = "http://localhost:52773/csp/healthshare/fhirserver/fhir/r4"
AUTH = base64.b64encode(b"superuser:SYS").decode()
H = {"Authorization": "Basic " + AUTH, "Accept": "application/fhir+json"}

req = urllib.request.Request(BASE + "/Patient?_count=10", headers=H)
bundle = json.load(urllib.request.urlopen(req, timeout=20))
print("total:", bundle.get("total"))
for e in bundle.get("entry", []):
    res = e.get("resource", {})
    ident = [i.get("value") for i in (res.get("identifier") or [])]
    print(" ", ident, "| name(utf8):", json.dumps(res.get("name"), ensure_ascii=False))
