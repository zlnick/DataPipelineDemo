# -*- coding: utf-8 -*-
"""列出 FHIR 各资源 id；识别并删除"非 UUID 格式"的旧残留（保留本次事务写入的 UUID 资源）。"""
import base64
import json
import re
import sys
import urllib.request

H = {"Authorization": "Basic " + base64.b64encode(b"superuser:SYS").decode(),
     "Accept": "application/fhir+json"}
B = "http://localhost:52773/csp/healthshare/fhirserver/fhir/r4/"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DO_DELETE = "--delete" in sys.argv


def get(u):
    return json.load(urllib.request.urlopen(urllib.request.Request(B + u, headers=H)))


for rt in ("Patient", "Encounter", "Condition", "MedicationRequest"):
    ids = [e["resource"]["id"] for e in (get(rt + "?_count=200&_elements=id").get("entry") or [])]
    old = [i for i in ids if not UUID_RE.match(i or "")]
    print(f"{rt}: total={len(ids)} uuid={len(ids) - len(old)} old={old}")
    if DO_DELETE and old:
        for i in old:
            try:
                urllib.request.urlopen(urllib.request.Request(B + rt + "/" + i,
                                                              headers=H, method="DELETE"))
            except Exception as exc:  # noqa: BLE001
                print("   delete failed:", i, exc)
        print("   deleted:", len(old))
