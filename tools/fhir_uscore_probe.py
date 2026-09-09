# -*- coding: utf-8 -*-
"""US Core 声明式支持探测：向 IRIS FHIR server 写入 4 类带 meta.profile 的资源并读回验证。"""
import base64
import json
import urllib.request

BASE = "http://127.0.0.1:52773/csp/healthshare/fhirserver/fhir/r4"
AUTH = "Basic " + base64.b64encode(b"superuser:SYS").decode()
UC = "http://hl7.org/fhir/us/core/StructureDefinition"


def call(method, url, payload=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/fhir+json")
    req.add_header("Authorization", AUTH)
    if payload is not None:
        req.add_header("Content-Type", "application/fhir+json")
        req.data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8")
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:500]}


RESOURCES = {
    "Patient/uscore-demo-001": {
        "resourceType": "Patient", "id": "uscore-demo-001",
        "meta": {"profile": [f"{UC}/us-core-patient"]},
        "identifier": [{"use": "usual", "system": "http://hospital.example/mrn", "value": "MRN-1001"}],
        "name": [{"use": "official", "family": "李", "given": ["伟"]}],
        "telecom": [{"system": "phone", "value": "13800000001", "use": "mobile"}],
        "gender": "male", "birthDate": "1985-04-12",
        "address": [{"line": ["示例路 1 号"], "city": "北京", "postalCode": "100000", "country": "CN"}],
    },
    "Encounter/uscore-demo-101": {
        "resourceType": "Encounter", "id": "uscore-demo-101",
        "meta": {"profile": [f"{UC}/us-core-encounter"]},
        "status": "in-progress",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                  "code": "IMP", "display": "inpatient encounter"},
        "type": [{"coding": [{"system": "http://snomed.info/sct", "code": "261665006",
                              "display": "Unknown (qualifier value)"}]}],
        "subject": {"reference": "Patient/uscore-demo-001"},
        "period": {"start": "2026-09-01T08:00:00+08:00"},
        "reasonCode": [{"coding": [{"system": "http://snomed.info/sct", "code": "44054006",
                                    "display": "Type 2 diabetes mellitus"}]}],
    },
    "Condition/uscore-demo-201": {
        "resourceType": "Condition", "id": "uscore-demo-201",
        "meta": {"profile": [f"{UC}/us-core-condition-encounter-diagnosis"]},
        "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                       "code": "active"}]},
        "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
                                           "code": "confirmed"}]},
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category",
                                  "code": "encounter-diagnosis"}]}],
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": "44054006",
                             "display": "Type 2 diabetes mellitus"}]},
        "subject": {"reference": "Patient/uscore-demo-001"},
        "encounter": {"reference": "Encounter/uscore-demo-101"},
        "onsetDateTime": "2026-08-15",
        "recordedDate": "2026-09-01",
    },
    "MedicationRequest/uscore-demo-301": {
        "resourceType": "MedicationRequest", "id": "uscore-demo-301",
        "meta": {"profile": [f"{UC}/us-core-medicationrequest"]},
        "status": "active", "intent": "order",
        "medicationCodeableConcept": {"coding": [
            {"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "6809",
             "display": "metformin"}]},
        "subject": {"reference": "Patient/uscore-demo-001"},
        "authoredOn": "2026-09-01",
        "requester": {"reference": "Practitioner/uscore-demo-prac"},
        "dosageInstruction": [{"text": "每日两次，每次 500 mg",
                               "timing": {"repeat": {"frequency": 2, "period": 1, "periodUnit": "d"}},
                               "route": {"coding": [{"system": "http://snomed.info/sct", "code": "26643006",
                                                     "display": "Oral route"}]},
                               "doseAndRate": [{"doseQuantity": {"value": 500, "unit": "mg",
                                                                 "system": "http://unitsofmeasure.org", "code": "mg"}}]}],
    },
}

print("== PUT 写入（声明 US Core profile） ==")
for path, res in RESOURCES.items():
    st, body = call("PUT", f"{BASE}/{path}", res)
    note = ""
    if isinstance(body, dict) and body.get("resourceType") == "OperationOutcome":
        diag = "; ".join(str(x.get("diagnostics") or x.get("details", {}).get("text", ""))
                         for x in body.get("issue", []))
        note = " -> " + diag[:200]
    print(f"{st}  {path}{note}")

print("\n== GET 读回验证 meta.profile ==")
for path in RESOURCES:
    st, body = call("GET", f"{BASE}/{path}")
    prof = body.get("meta", {}).get("profile", []) if isinstance(body, dict) else []
    print(f"{st}  {path}  profile={prof}")

print("\n== 检索验证 ==")
for rt in ["Patient", "Condition", "MedicationRequest"]:
    st, body = call("GET", f"{BASE}/{rt}?subject:missing=false&_count=3")
    if isinstance(body, dict) and body.get("resourceType") == "Bundle":
        print(f"  {rt}: total={body.get('total')} 样例={[e['resource'].get('id') for e in body.get('entry', [])][:3]}")
    else:
        print(f"  {rt}: HTTP {st}")
