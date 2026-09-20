"""已封装 AI Agent 目录 API。"""

from flask import Blueprint

from backend.services import agents as agent_registry
from backend.utils import success

agents_bp = Blueprint("agents", __name__, url_prefix="/api/agents")


@agents_bp.get("")
def list_agents():
    """已封装 Agent 列表（前端「AI Agents」页）。"""
    return success({"items": agent_registry.list_agents()})


@agents_bp.get("/skills")
def list_skills():
    """**Skill 目录**（AI 决策用的受控目录，前端「AI Agents」页 Skills 目录卡片）。

    · design：管道设计 Skill（`pipeline_design_skills.DESIGN_SKILLS`）—— 由 pipeline-agent
      （数据管道设计 Agent）按「源/目标对」选择；含适用对、状态、拓扑角色与实际使用次数。
    · term：术语判码 Skill（`transform_directives.TERM_SKILLS`）—— 由 transformation-agent(A)
      / C1 以受控指令 `term_map:<skill>` 注入；含源/目标体系与实际使用次数。
    口径：平台只按目录**参数化**，决策仍归 AI（目录是"可选清单"，不是写死规则）。
    """
    from backend.services import pipeline_design_skills as pds
    from backend.services import pipeline_instances as pinst
    from backend.services import repository as repo
    from backend.services.transform_directives import TERM_SKILLS

    design_used: dict = {}
    try:
        for r in pinst.list_instances():
            k = str(r.get("design_skill") or "")
            if k:
                design_used[k] = design_used.get(k, 0) + 1
    except Exception:  # noqa: BLE001 - 统计失败不影响目录展示
        pass
    term_used: dict = {}
    try:
        for m in repo.list_mappings():
            for f in (m.get("field_mappings") or []):
                t = str(f.get("transform") or "")
                if t.startswith("term_map:"):
                    k = t.split(":", 1)[1]
                    term_used[k] = term_used.get(k, 0) + 1
    except Exception:  # noqa: BLE001
        pass

    design = [{
        "id": s.get("id"), "name": s.get("name"), "role": s.get("role"),
        "purpose": s.get("purpose"), "applies_to": s.get("applies_to") or {},
        "status": s.get("status"), "trigger": s.get("trigger"),
        "topology": [str(t.get("role")) for t in (s.get("topology_spec") or [])],
        "used_by": "pipeline-agent（数据管道设计 Agent）",
        "used_count": int(design_used.get(str(s.get("id")), 0)),
    } for s in pds.DESIGN_SKILLS]

    term = [{
        "id": sid, "name": v.get("label"), "label": v.get("label"),
        "source_system": v.get("source_system"), "target_system": v.get("target_system"),
        "agent": v.get("agent"),
        "used_by": "transformation-agent（A）/ C1（受控指令 term_map:%s）" % sid,
        "used_count": int(term_used.get(sid, 0)),
    } for sid, v in TERM_SKILLS.items()]

    return success({"design": design, "term": term,
                    "note": "Skill 目录 = AI 决策的受控清单（平台只参数化，不写死决策）；"
                            "used_count 为当前环境实际使用次数（重置后归零）"})
