# -*- coding: utf-8 -*-
"""核实新患者（MRN-1003）整户资源及其引用关联。"""
import base64
import json
import sys
import urllib.request

MRN = sys.argv[1] if len(sys.argv) > 1 else "MRN-1004"
H = {"Authorization": "Basic " + base64.b64encode(b"superuser:SYS").decode(),
     "Accept": "application/fhir+json"}
B = "http://localhost:52773/csp/healthshare/fhirserver/fhir/r4/"


def get(u):
    return json.load(urllib.request.urlopen(urllib.request.Request(B + u, headers=H)))


bundle = get("Patient?identifier=" + MRN)
entry = bundle.get("entry") or []
if not entry:
    # 兼容 identifier 未入索引时按 name 检索
    bundle = get("Patient?name=%E5%88%98")
    entry = bundle.get("entry") or []
print("Patient found:", len(entry))
for e in entry:
    p = e["resource"]
    pid = p.get("id")
    print("  Patient", pid, p.get("name"), "| gender:", p.get("gender"),
          "| birthDate:", p.get("birthDate"))
    for rt in ("Encounter", "Condition", "MedicationRequest"):
        sub = get(f"{rt}?subject=Patient/{pid}")
        items = sub.get("entry") or []
        print(f"    {rt}: {len(items)}")
        for it in items:
            r = it["resource"]
            refs = {"subject": (r.get("subject") or {}).get("reference"),
                    "encounter": (r.get("encounter") or {}).get("reference")}
            extra = ""
            if rt == "Encounter":
                extra = f"| class={r.get('class')} | period={r.get('period')}"
            if rt == "Condition":
                extra = f"| code={r.get('code')}"
            if rt == "MedicationRequest":
                extra = f"| medication={r.get('medicationCodeableConcept')} | intent={r.get('intent')}"
            print(f"      {r.get('id')} refs={refs}{extra}")
