# -*- coding: utf-8 -*-
"""Ens 事件日志尾部（带列名）+ 消息 390 全字段。"""
import iris.dbapi as D

conn = D.connect(hostname="iris", port=1972, namespace="USER",
                 username="superuser", password="SYS")
cur = conn.cursor()
try:
    cur.execute("SELECT TOP 3 * FROM Ens_Util.Log ORDER BY ID DESC")
    cols = [d[0] for d in cur.description]
    print("LOG cols:", cols)
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        print({k: str(d.get(k))[:300] for k in ("Type", "ConfigName", "Text", "TimeLogged")
               if k in d})
        print("---")
except Exception as exc:  # noqa
    print("log query err:", exc)

try:
    cur.execute("SELECT TOP 1 * FROM Ens.MessageHeader WHERE ID=390")
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    d = dict(zip(cols, row)) if row else {}
    print("HDR cols with values:", {k: str(v)[:200] for k, v in d.items() if v not in (None, "")})
except Exception as exc:  # noqa
    print("hdr query err:", exc)
conn.close()



