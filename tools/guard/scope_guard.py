#!/usr/bin/env python3
"""路径边界守卫（Path Scope Guard）——任何"清理/删除/写备份"脚本动手前的强制校验。

设计目标（对应 2026-09-14 越界事故）：
    把「AI 只能写/删 本项目仓库 + 知识库」从口头约束变成**机器校验**：
    越界路径一律拒绝对其执行任何破坏性操作。

允许写入的根（ALLOWED_ROOTS）：
    1. 本项目仓库根（本文件的上两级目录，即 dirname(dirname(dirname(__file__)))）
    2. 知识库 `~/Documents/IRIS-Dev-Vault`（经仓库内 `./knowledge` 软链解析后的真实路径）
    3. 系统临时目录（`/tmp`、macOS 的 `/private/tmp`）——仅用于会话内临时产物

用法（CLI）：
    python3 tools/guard/scope_guard.py roots                # 打印允许的根
    python3 tools/guard/scope_guard.py check <路径>...       # 全部在范围内 → 退出码 0；任一越界 → 1

用法（作为库，供清理脚本调用）：
    from scope_guard import assert_in_scope   # 或 sys.path.insert 后 import
    assert_in_scope([p for p in candidates])  # 越界即抛 ScopeViolation，脚本直接中止
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Iterable, List, Tuple


class ScopeViolation(RuntimeError):
    """目标路径超出允许范围时抛出。"""


def _repo_root() -> Path:
    """本文件位于 <repo>/tools/guard/scope_guard.py。"""
    return Path(__file__).resolve().parents[2]


def allowed_roots() -> List[Path]:
    """返回允许写入/删除的根目录（已 resolve，去重保序）。"""
    roots: List[Path] = [_repo_root()]

    # 知识库：优先取仓库内软链 ./knowledge 的真实目标
    link = _repo_root() / "knowledge"
    try:
        if link.exists():
            roots.append(link.resolve())
    except OSError:
        pass

    # 临时目录（macOS 上 /tmp 是 /private/tmp 的软链，两者都收）
    for candidate in (tempfile.gettempdir(), "/tmp", "/private/tmp"):
        try:
            roots.append(Path(candidate).resolve())
        except OSError:
            continue

    seen, ordered = set(), []
    for root in roots:
        if root not in seen:
            seen.add(root)
            ordered.append(root)
    return ordered


def check_path(path: str | os.PathLike) -> Tuple[bool, str, str]:
    """校验单个路径。

    参数:
        path: 待校验路径（相对路径按当前工作目录解析）。

    返回:
        (是否允许, 命中的范围根, 说明)；不允许时范围根为空串。
    """
    target = Path(path).expanduser()
    try:
        resolved = target.resolve()
    except OSError as exc:  # 路径无法解析（权限/断链等）→ 保守拒绝
        return False, "", f"路径无法解析（{exc}）"

    for root in allowed_roots():
        if resolved == root or root in resolved.parents:
            scope = "仓库" if root == _repo_root() else ("知识库" if "IRIS-Dev-Vault" in str(root) else "临时目录")
            return True, str(root), f"在允许范围内（{scope}）：{resolved}"
    return False, "", f"越界：{resolved} 不在仓库/知识库/临时目录之内"


def assert_in_scope(paths: Iterable[str | os.PathLike]) -> None:
    """批量校验；任一越界即抛 ScopeViolation（清理脚本应在此之前中止）。"""
    violations = []
    for item in paths:
        ok, _, reason = check_path(item)
        if not ok:
            violations.append(reason)
    if violations:
        raise ScopeViolation("；".join(violations))


def main(argv: List[str]) -> int:
    """CLI 入口：roots / check。"""
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0 if len(argv) >= 2 else 2

    action = argv[1]
    if action == "roots":
        for root in allowed_roots():
            print(root)
        return 0

    if action == "check":
        if len(argv) < 3:
            print("用法：python3 tools/guard/scope_guard.py check <路径>...", file=sys.stderr)
            return 2
        bad = 0
        for item in argv[2:]:
            ok, _, reason = check_path(item)
            print(f"[{'OK  ' if ok else 'DENY'}] {reason}")
            bad += 0 if ok else 1
        return 1 if bad else 0

    print(f"未知动作：{action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
