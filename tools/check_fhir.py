# -*- coding: utf-8 -*-
"""校验 FHIR 落地：各资源数量 + Patient id 是否确定性 UUID。"""
import base64
import json
import urllib.request

H = {"Authorization": "Basic " + base64.b64encode(b"superuser:SYS").decode(),
     "Accept": "application/fhir+json"}
BASE = "http://localhost:52773/csp/healthshare/fhirserver/fhir/r4/"


def get(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=H)))


for rt in ("Patient", "Encounter", "Condition", "MedicationRequest"):
    try:
        print(rt, get(BASE + rt + "?_summary=count").get("total"))
    except Exception as exc:  # noqa
        print(rt, "ERR", exc)

d = get(BASE + "Patient?_count=3")
for e in d.get("entry", []):
    r = e["resource"]
    print("Patient id=", r.get("id"), "| identifier=", r.get("identifier"),
          "| name=", (r.get("name") or [{}])[0])

c = get(BASE + "Condition?_count=2")
for e in c.get("entry", []):
    r = e["resource"]
    print("Condition id=", r.get("id"), "| code=", r.get("code"),
          "| subject=", r.get("subject"), "| encounter=", r.get("encounter"))
