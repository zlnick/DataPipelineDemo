# -*- coding: utf-8 -*-
"""验证 Ens 清理类 API 的调用方式：classMethodValue vs classMethodVoid。

背景：`Ens.Adapter.ClearStaticAppData` 这类「无返回值」类方法用 classMethodValue 调用会报
`<COMMAND> *Function must return a value at ClearStaticAppData+1^Ens.Adapter.1`
（容易被误判为"库方法在本版本无效"）→ 改用 classMethodVoid 才是正确姿势。

本脚本用**独立测试 Item 名**（不影响任何在跑的 Production），验证：
  set last key → 读回 → 清理 → 再读回，并清理测试残留 global。
"""
import iris
from backend.services import iris_connector as ic

ITEM = "ZZTest_ItemOnly"


def last_key(item):
    try:
        return ic.class_method_value("EnsLib.SQL.InboundAdapter", "GetPersistentValue",
                                     item, "%LastKey")
    except Exception as exc:  # noqa: BLE001
        return f"<读取失败: {str(exc)[:80]}>"


conn = ic.get_connection()
try:
    native = iris.createIRIS(conn)
    print("初始 last key:", last_key(ITEM))

    print("\n[1] classMethodVoid 设 last key = K9 …")
    try:
        native.classMethodVoid("EnsLib.SQL.InboundAdapter", "InitializeLastKeyValue",
                               ITEM, "K9")
        print("    ✓ 调用成功；读回 =", last_key(ITEM))
    except Exception as exc:  # noqa: BLE001
        print("    ✗ classMethodVoid 失败:", str(exc)[:160])

    print("\n[2] classMethodVoid ClearStaticAppData …")
    try:
        native.classMethodVoid("Ens.Adapter", "ClearStaticAppData", ITEM)
        print("    ✓ 调用成功；读回 =", last_key(ITEM))
    except Exception as exc:  # noqa: BLE001
        print("    ✗ classMethodVoid 失败:", str(exc)[:160])

    print("\n[3] 对照：classMethodValue 调同方法（预期报错）…")
    try:
        print("    value 调用返回:", ic.class_method_value("Ens.Adapter", "ClearStaticAppData", ITEM))
    except Exception as exc:  # noqa: BLE001
        print("    ✗ 预期报错:", str(exc)[:140])

    for g, subs in (("^Ens.AppData", (ITEM,)), ("^IRIS.Temp.Adapter.sqlrow", (ITEM,))):
        try:
            native.kill(g, *subs)
        except Exception as exc:  # noqa: BLE001
            print("清理残留失败:", exc)
    print("\n测试残留已清理")
finally:
    conn.close()
