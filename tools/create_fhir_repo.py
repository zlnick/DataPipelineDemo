#!/usr/bin/env python3
"""创建 / 自检第二个 FHIR 存储库（DemoFHIR 命名空间 + 独立 FHIR 仓库端点）。

设计（单一事实源，避免逻辑漂移）：
    `iris/setup.sh` 的「2b」步骤 = 容器每次启动都会执行的创建脚本；
    本工具把**同一段 ObjectScript**（heredoc 内容）抽出来，在**正在运行的** dataflow-iris 上幂等执行，
    因此「现在手动创建」与「容器重建后自动创建」永远是同一份逻辑。

自检覆盖（除写入一条测试 Patient 并清理外，均为只读）：
    ① IRIS 侧：命名空间 / 主库 / 仓库数据仓库库(<ns>X0001R|V) / CSP 应用属性 / 数据库目录（持久化）
    ② HTTP 侧：`/metadata`（CapabilityStatement + fhirVersion）→ PUT → GET（含中文回读）
       → 与既有仓库的**隔离性**（同 id 在既有仓库应 404）→ DELETE 清理

用法：
    python3 tools/create_fhir_repo.py                # 执行创建（幂等）+ 自检
    python3 tools/create_fhir_repo.py --check        # 只自检，不改动环境
    python3 tools/create_fhir_repo.py --keep         # 自检后保留写入的测试 Patient
    python3 tools/create_fhir_repo.py --host http://localhost:52773 --ref-endpoint /csp/healthshare/fhirserver/fhir/r4
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SETUP_SH = REPO / "iris" / "setup.sh"
CONTAINER = "dataflow-iris"
IRIS_INSTANCE = "IRIS"
AUTH = "Basic " + base64.b64encode(b"superuser:SYS").decode()
TEST_ID = "demo-repo-check-001"
TEST_PAYLOAD = {
    "resourceType": "Patient",
    "id": TEST_ID,
    "name": [{"family": "张", "given": ["伟芳"]}],
    "gender": "male",
    "birthDate": "1980-05-06",
}
HEREDOC_HEAD = "cat << 'EOF' > /tmp/setup_demofhir.os"


def extract_script() -> str:
    """从 iris/setup.sh 抽出 2b 步骤的 ObjectScript（heredoc 内容，单一事实源）。"""
    lines = SETUP_SH.read_text(encoding="utf-8").splitlines()
    begin = next((i + 1 for i, ln in enumerate(lines) if ln.strip() == HEREDOC_HEAD), None)
    if begin is None:
        raise SystemExit(f"✗ 未在 {SETUP_SH} 中找到 2b 步骤（{HEREDOC_HEAD}）")
    for idx in range(begin, len(lines)):
        if lines[idx].strip() == "EOF":
            return "\n".join(lines[begin:idx]) + "\n"
    raise SystemExit(f"✗ {SETUP_SH} 中 2b 步骤 heredoc 未闭合")


def script_params(script: str) -> dict:
    """从脚本自身解析 ns / 端点 —— 保证「被创建的对象」与「被检查的对象」一致。"""
    ns = re.search(r'^set ns = "([^"]+)"', script, re.M)
    app = re.search(r'^set appKey = "([^"]+)"', script, re.M)
    if not (ns and app):
        raise SystemExit("✗ 2b 脚本中缺少 ns / appKey 定义")
    return {"ns": ns.group(1), "endpoint": app.group(1)}


def run_in_iris(os_script: str) -> str:
    """把 ObjectScript 灌进 IRIS 容器并执行（stdin 传脚本，避开 docker cp 的路径边界问题）。"""
    cmd = [
        "docker", "exec", "-i", CONTAINER, "sh", "-c",
        "cat > /tmp/create_fhir_repo.os; "
        f"/usr/irissys/bin/iris session {IRIS_INSTANCE} -U %SYS < /tmp/create_fhir_repo.os",
    ]
    proc = subprocess.run(cmd, input=os_script, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise SystemExit(f"✗ 容器内执行失败（rc={proc.returncode}）:\n{out}")
    return out


def iris_state(ns: str, endpoint: str) -> dict:
    """只读检查：命名空间 / 数据库 / 仓库库 / CSP 应用属性 / 数据库目录。"""
    os_script = (
        'zn "%SYS"\n'
        f'write "CHECK_ns=", ##class(%SYS.Namespace).Exists("{ns}"), !\n'
        f'write "CHECK_db=", ##class(Config.Databases).Exists("{ns}"), !\n'
        f'write "CHECK_r=", ##class(Config.Databases).Exists("{ns}X0001R"), !\n'
        f'write "CHECK_v=", ##class(Config.Databases).Exists("{ns}X0001V"), !\n'
        f'write "CHECK_app=", ##class(Security.Applications).Exists("{endpoint}"), !\n'
        f'do ##class(Security.Applications).Get("{endpoint}", .ap)\n'
        'write "CHECK_app_ns=", $g(ap("NameSpace")), !\n'
        'write "CHECK_app_dispatch=", $g(ap("DispatchClass")), !\n'
        'write "CHECK_app_authe=", $g(ap("AutheEnabled")), !\n'
        'write "CHECK_app_roles=", $g(ap("MatchRoles")), !\n'
        f'do ##class(Config.Databases).Get("{ns}", .dp)\n'
        'write "CHECK_dir=", $g(dp("Directory")), !\n'
        "halt\n"
    )
    out = run_in_iris(os_script)
    state = {}
    for match in re.finditer(r"^CHECK_([A-Za-z_]+)=(.*)$", out, re.M):
        state[match.group(1)] = match.group(2).strip()
    if not state:
        raise SystemExit(f"✗ 未能解析 IRIS 检查输出:\n{out}")
    return state


def fhir_call(method: str, url: str, body: bytes | None = None) -> tuple[int, str]:
    """简单 FHIR REST 调用：返回 (HTTP 状态码, 响应文本)。"""
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", AUTH)
    req.add_header("Accept", "application/fhir+json")
    if body is not None:
        req.add_header("Content-Type", "application/fhir+json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="创建/自检第二个 FHIR 存储库（DemoFHIR）")
    parser.add_argument("--check", action="store_true", help="只自检，不执行创建")
    parser.add_argument("--keep", action="store_true", help="自检后保留测试 Patient（默认删除）")
    parser.add_argument("--host", default="http://localhost:52773", help="IRIS Web 网关地址")
    parser.add_argument("--ref-endpoint", default="/csp/healthshare/fhirserver/fhir/r4",
                        help="既有 FHIR 仓库端点（隔离性对照；置空则跳过该检查）")
    args = parser.parse_args()

    script = extract_script()
    params = script_params(script)
    ns, endpoint = params["ns"], params["endpoint"]
    print(f"== 目标：命名空间 {ns} / 端点 {endpoint}（取自 iris/setup.sh 2b 步骤）==")

    if args.check:
        print("-- 只自检模式（未改动环境）--")
    else:
        print("-- 执行创建（幂等；运行中的实例无需重启）--")
        out = run_in_iris(script)
        for line in out.splitlines():
            if "[demofhir]" in line:
                print("   " + line.strip())
        if "DEMOFHIR_FHIR_REPO_DONE" not in out:
            print("✗ 创建脚本未打印完成标记，请检查上面的输出")
            return 1

    results: list[tuple[bool, str]] = []
    state = iris_state(ns, endpoint)
    results.append((state.get("ns") == "1", f"命名空间 {ns} 存在（{state.get('ns')}）"))
    results.append((state.get("db") == "1", f"主数据库 {ns} 存在（{state.get('db')}）"))
    results.append((state.get("r") == "1", f"仓库数据数据库 {ns}X0001R 存在（{state.get('r')}）"))
    results.append((state.get("v") == "1", f"仓库数据数据库 {ns}X0001V 存在（{state.get('v')}）"))
    results.append((str(state.get("dir", "")).startswith("/dur"),
                    f"主数据库目录在持久化卷内（{state.get('dir')}）"))
    results.append((state.get("app") == "1", f"CSP 应用 {endpoint} 存在（{state.get('app')}）"))
    results.append((state.get("app_ns") == ns, f"端点归属命名空间 = {ns}（{state.get('app_ns')}）"))
    results.append((state.get("app_dispatch") == "HS.FHIRServer.RestHandler",
                    f"端点分发类 = HS.FHIRServer.RestHandler（{state.get('app_dispatch')}）"))
    results.append((state.get("app_authe") == "8288" and state.get("app_roles") == ":%All",
                    "端点认证与既有 FHIR 端点同口径"
                    f"（AutheEnabled={state.get('app_authe')}, MatchRoles={state.get('app_roles')}）"))


    base = args.host.rstrip("/")
    url = base + endpoint
    status, body = fhir_call("GET", f"{url}/metadata")
    fhir_version = ""
    ok_meta = status == 200 and "CapabilityStatement" in body
    if ok_meta:
        try:
            fhir_version = json.loads(body).get("fhirVersion", "")
        except json.JSONDecodeError:
            ok_meta = False
    results.append((ok_meta, f"GET {endpoint}/metadata → HTTP {status}（fhirVersion={fhir_version or 'n/a'}）"))

    status, _ = fhir_call("PUT", f"{url}/Patient/{TEST_ID}", json.dumps(TEST_PAYLOAD).encode("utf-8"))
    results.append((status in (200, 201), f"PUT Patient/{TEST_ID} → HTTP {status}（期望 200/201）"))

    status, body = fhir_call("GET", f"{url}/Patient/{TEST_ID}")
    chinese_ok = "伟芳" in body
    results.append((status == 200 and chinese_ok,
                    f"GET Patient/{TEST_ID} → HTTP {status}，中文回读{'正确' if chinese_ok else '异常'}"))

    if args.ref_endpoint:
        status, _ = fhir_call("GET", f"{base}{args.ref_endpoint}/Patient/{TEST_ID}")
        results.append((status in (404, 410),
                        f"隔离性：同 id 在既有仓库 {args.ref_endpoint} → HTTP {status}（期望 404/410）"))

    if not args.keep:
        status, _ = fhir_call("DELETE", f"{url}/Patient/{TEST_ID}")
        results.append((status in (200, 204), f"清理测试 Patient（DELETE）→ HTTP {status}（期望 200/204）"))
        status, _ = fhir_call("GET", f"{url}/Patient/{TEST_ID}")
        results.append((status in (404, 410), f"清理后读回 → HTTP {status}（期望 404/410）"))

    print()
    failed = 0
    for ok, desc in results:
        print(f"  {'✅' if ok else '❌'} {desc}")
        failed += 0 if ok else 1
    total = len(results)
    tail = "（全部通过）" if not failed else f"，{failed} 项失败"
    print(f"\n== 自检结果：{total - failed}/{total} 通过{tail} ==")
    if not failed:
        print(f"   地址：{base}{endpoint}/  （Basic Auth superuser/SYS；仅 /metadata 匿名）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

