"""CapabilityStatement 分析：识别 FHIR 端点采用的资源类型与 Profile。"""

from typing import Any


def analyze_capability(cap: dict) -> dict:
    """从 CapabilityStatement 提取资源类型、支持的操作与 Profile 信息。

    参数:
        cap: FHIR CapabilityStatement（dict）。

    返回:
        分析结果 dict：
        {
          "fhir_version": "4.0.1",
          "status": "active",
          "resource_types": ["Account", "Patient", ...],
          "supported_profiles": ["http://...", ...],
          "resource_count": 145,
        }
    """
    resources: list[str] = []
    supported_profiles: set[str] = set()
    interactions: set[str] = set()

    for rest in cap.get("rest", []):
        for res in rest.get("resource", []):
            rtype = res.get("type")
            if rtype:
                resources.append(rtype)
            for prof in res.get("supportedProfile", []) or []:
                if isinstance(prof, str):
                    supported_profiles.add(prof)
        for inter in rest.get("interaction", []) or []:
            code = inter.get("code")
            if code:
                interactions.add(code)

    # 汇总 Profile：提取常见 US Core / 其他基线的简写
    profiles = sorted(supported_profiles)
    profile_hint = []
    for p in profiles:
        if "us-core" in p.lower():
            profile_hint.append("US Core")
        elif "structuredefinition" in p.lower() and "fhir" in p:
            profile_hint.append("HL7 标准 Profile")

    return {
        "fhir_version": cap.get("fhirVersion", ""),
        "status": cap.get("status", ""),
        "resource_types": sorted(set(resources)),
        "supported_profiles": profiles,
        "profile_hint": sorted(set(profile_hint)) if profile_hint else ["标准 FHIR（无显式 profile）"],
        "resource_count": len(set(resources)),
        "interactions": sorted(interactions),
    }


def sample_profile_from_resources(resources: list[dict]) -> str:
    """从资源样本的 meta.profile 判断端点采用的 Profile（辅助）。

    参数:
        resources: FHIR 资源列表。

    返回:
        Profile 摘要字符串。
    """
    profiles: set[str] = set()
    for r in resources:
        for prof in (r.get("meta") or {}).get("profile", []) or []:
            profiles.add(prof)
    return "；".join(sorted(profiles)) or "无（标准 R4）"
