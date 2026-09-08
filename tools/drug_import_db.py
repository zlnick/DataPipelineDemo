"""通过 DB-API 将药品目录 TSV 导入 iris-terminology（Terminology_Drug.Code）。"""
import csv
import os

import iris.dbapi

HOST = "127.0.0.1"
PORT = 51774
NS = "TERMINOLOGY"


def clean(v):
    return str(v or "").replace("\x00", "")


def nn(v):
    """空字符串转 None（NULL），避免 IRIS 把空串读回 \u0000。"""
    c = clean(v)
    return c if c else None


def main():
    conn = iris.dbapi.connect(hostname=HOST, port=PORT, namespace=NS,
                              username="superuser", password="SYS")
    cur = conn.cursor()
    cur.execute("DELETE FROM Terminology_Drug.Code")
    total = 0
    for name in ("nrdl", "cbih"):
        path = f"/Users/lzhu/Documents/GeneratorDemo/data/terms-inbox/{name}.tsv"
        n = 0
        with open(path, encoding="utf-8") as fh:
            reader = csv.reader(fh, delimiter="\t")
            header = next(reader, None)
            for row in reader:
                if len(row) < 9:
                    continue
                try:
                    c1, c2 = row[0], row[1]
                    c3 = clean(row[2])[:150]
                    c4, c5, c6 = nn(row[3]), nn(row[4]), nn(row[5])
                    c7, c8, c9 = nn(row[6]), nn(row[7]), int(row[8] or 0)
                    cur.execute(
                        "INSERT OR UPDATE INTO Terminology_Drug.Code "
                        "(CodeSystemId, Code, Display, Form, Note, Category, SectionName, Extras, SrcPage) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (c1, c2, c3,
                         c4[:110] if c4 else None, c5[:400] if c5 else None,
                         c6[:70] if c6 else None, c7[:35] if c7 else None,
                         c8[:1500] if c8 else None, c9))
                except Exception as exc:  # noqa: BLE001 - 单行失败跳过，不中断整批
                    if n == 0:
                        print("  行跳过示例:", str(exc)[:140], "|", row[1], row[2])
                    continue
                n += 1
        total += n
        print(f"{name}: {n} 行")
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM Terminology_Drug.Code")
    print("总数:", cur.fetchone()[0])
    conn.close()


if __name__ == "__main__":
    main()
