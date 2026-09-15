# -*- coding: utf-8 -*-
"""遍历 ^Ens.AppData 与 ^IRIS.Temp.Adapter.sqlrow，定位 SQL 源凭证/已处理行/错误行。

（iris session 有行长限制，长 ObjectScript 无法执行 → 用 Native SDK 读 global。）
"""
import iris

from backend.services.iris_connector import get_connection


def walk(native, name, subs, depth=0, max_depth=4, out=None):
    """递归打印 global 子树（限制深度，避免全库遍历）。"""
    if out is None:
        out = []
    for sub in native.iterator(name, *subs):
        path = subs + [sub]
        try:
            val = native.get(name, *path)
        except Exception as exc:  # noqa: BLE001
            val = f"<err {exc}>"
        if val is not None and val != "":
            out.append(("  " * depth) + "|".join(str(p) for p in path) + " = " + repr(val))
        else:
            out.append(("  " * depth) + "|".join(str(p) for p in path))
        if depth + 1 < max_depth:
            walk(native, name, path, depth + 1, max_depth, out)
    return out


conn = get_connection()
try:
    native = iris.createIRIS(conn)
    for gname, subs in (("^Ens.AppData", []),
                        ("^Ens.AppData", ["Item"]),
                        ("^IRIS.Temp.Adapter.sqlrow", [])):
        print(f"==== {gname}{subs} ====")
        try:
            for line in walk(native, gname, subs):
                print(line)
        except Exception as exc:  # noqa: BLE001
            print("  读取失败:", exc)
finally:
    conn.close()
