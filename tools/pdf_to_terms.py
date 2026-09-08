"""把医保/商保药品目录 CSV 规整为术语数据（可复现，幂等 code=hash(name+剂型)）。

输入: ~/Downloads/{医保目录,商保目录}_table.csv（pdf_convert*.py 产物）
输出: data/terms-inbox/nrdl.tsv、data/terms-inbox/cbih.tsv
字段: CodeSystemId | Code | Display | Form | Note | Category | Section | Extras(JSON) | SrcPage
"""
import csv
import hashlib
import json
import os

OUT = os.path.join(os.path.dirname(__file__), "..", "data", "terms-inbox")


def code(csid, name, extra=""):
    h = hashlib.sha1((name + "|" + extra).encode("utf-8")).hexdigest()[:10].upper()
    return f"{csid}-{h}"


def section_of(page):
    if page < 82:
        return "西药"
    if page < 125:
        return "中成药"
    if page < 191:
        return "谈判"
    return "中药饮片"


def load_nrdl(path):
    rows = list(csv.reader(open(path, encoding="utf-8-sig")))
    h = rows[0]
    body = rows[1:]
    name_i = next(i for i, c in enumerate(h) if "名称" in c)
    dose_i = name_i + 1
    note_i = name_i + 2
    code_i = 1
    out = []
    for r in body:
        if name_i >= len(r) or not r[name_i].strip():
            continue
        name = r[name_i].strip()
        dose = r[dose_i].strip() if dose_i < len(r) else ""
        note = r[note_i].strip() if note_i < len(r) else ""
        cat = r[code_i].strip() if code_i < len(r) else ""
        if not name:
            continue
        out.append({
            "CodeSystemId": "nrdl",
            "Code": code("NRDL", name, dose),
            "Display": name,
            "Form": dose,
            "Note": note,
            "Category": cat,
            "Section": section_of(int(r[0])),
            "Extras": {},
            "SrcPage": r[0],
        })
    return out


def load_cbih(path):
    rows = list(csv.reader(open(path, encoding="utf-8-sig")))
    h = rows[0]
    body = rows[1:]
    name_i = next(i for i, c in enumerate(h) if "名称" in c)
    brand_i = name_i + 1
    ind_i = name_i + 2
    mfr_i = ind_i + 1
    auth_i = mfr_i + 1
    valid_i = auth_i + 1
    out = []
    for r in body:
        if name_i >= len(r) or not r[name_i].strip():
            continue
        name = r[name_i].strip()
        if not name:
            continue
        out.append({
            "CodeSystemId": "cbih",
            "Code": code("CB", name),
            "Display": name,
            "Form": "",
            "Note": "",
            "Category": "",
            "Section": "商保创新药",
            "Extras": {
                "brand": r[brand_i].strip() if brand_i < len(r) else "",
                "indication": r[ind_i].strip() if ind_i < len(r) else "",
                "holder": r[mfr_i].strip() if mfr_i < len(r) else "",
                "authorized": r[auth_i].strip() if auth_i < len(r) else "",
                "valid": r[valid_i].strip() if valid_i < len(r) else "",
            },
            "SrcPage": r[0],
        })
    return out


def write_tsv(items, name):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("CodeSystemId\tCode\tDisplay\tForm\tNote\tCategory\tSection\tExtras\tSrcPage\n")
        for it in items:
            fh.write("\t".join([
                it["CodeSystemId"], it["Code"], it["Display"], it["Form"],
                it["Note"], it["Category"], it["Section"],
                json.dumps(it["Extras"], ensure_ascii=False), str(it["SrcPage"]),
            ]) + "\n")
    return path


if __name__ == "__main__":
    nrdl = load_nrdl(os.path.expanduser("~/Downloads/医保目录_table.csv"))
    cbih = load_cbih(os.path.expanduser("~/Downloads/商保目录_table.csv"))
    p1 = write_tsv(nrdl, "nrdl.tsv")
    p2 = write_tsv(cbih, "cbih.tsv")
    from collections import Counter
    print("nrdl 条目:", len(nrdl), "分节:", dict(Counter(x["Section"] for x in nrdl)))
    print("cbih 条目:", len(cbih))
    print("输出:", p1, "|", p2)
    for it in nrdl[:3] + cbih[:3]:
        print("  ", it["CodeSystemId"], it["Code"], "|", it["Display"], "|", it["Form"], "|", it["Section"])
