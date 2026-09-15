# -*- coding: utf-8 -*-
"""删除探针患者（保持演示数据干净）。"""
import base64
import urllib.error
import urllib.request

BASE = "http://localhost:52773/csp/healthshare/fhirserver/fhir/r4"
AUTH = base64.b64encode(b"superuser:SYS").decode()
H = {"Authorization": "Basic " + AUTH, "Accept": "application/fhir+json"}
req = urllib.request.Request(BASE + "/Patient/11111111-2222-3333-4444-555555555555",
                             headers=H, method="DELETE")
try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        print("delete status:", resp.status)
except urllib.error.HTTPError as exc:
    print("delete error:", exc.code, exc.read().decode("utf-8", "replace")[:200])
