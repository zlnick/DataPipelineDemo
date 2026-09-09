# -*- coding: utf-8 -*-
"""中文药品名 ↔ RxNorm 别名映射表灌数（演示）。

对每个内置中文名，到 Terminology_Rxnorm.Concept 精确 resolve 英文成分名：
命中 -> 写入 Terminology_Rxnorm.ChineseAlias（幂等重建）；
未命中（如国产药不在 RxNorm）-> 仅打印清单，不写表。

用法: python3 tools/rxnorm_zhmap.py
"""
import iris.dbapi

HOST, PORT, NS = "127.0.0.1", 51774, "TERMINOLOGY"

# (中文名, RxNorm 英文名, 来源说明)
CANDIDATES = [
    # 通用名/难例（医保目录）
    ("阿托伐他汀", "atorvastatin", "医保目录(立普妥)"),
    ("硝苯地平", "nifedipine", "医保目录"),
    ("二甲双胍", "metformin", "医保目录"),
    ("阿莫西林", "amoxicillin", "医保目录"),
    ("阿奇霉素", "azithromycin", "医保目录"),
    ("氯吡格雷", "clopidogrel", "医保目录"),
    ("奥美拉唑", "omeprazole", "医保目录"),
    ("布洛芬", "ibuprofen", "医保目录"),
    ("甘精胰岛素", "insulin glargine", "医保目录(来得时)"),
    # 商保/肿瘤创新药（音译名，跨语言向量易错）
    ("阿基仑赛", "axicabtagene ciloleucel", "商保目录(奕凯达/Yescarta)"),
    ("帕博利珠单抗", "pembrolizumab", "商保(可瑞达/Keytruda)"),
    ("纳武利尤单抗", "nivolumab", "商保(欧狄沃/Opdivo)"),
    ("曲妥珠单抗", "trastuzumab", "商保(赫赛汀/Herceptin)"),
    ("贝伐珠单抗", "bevacizumab", "商保(安维汀/Avastin)"),
    ("利妥昔单抗", "rituximab", "医保(美罗华/MabThera)"),
    ("西妥昔单抗", "cetuximab", "医保(爱必妥/Erbitux)"),
    ("奥希替尼", "osimertinib", "商保(泰瑞沙/Tagrisso)"),
    ("阿来替尼", "alectinib", "商保(安圣莎/Alecensa)"),
    ("克唑替尼", "crizotinib", "商保(赛可瑞/Xalkori)"),
    ("伊布替尼", "ibrutinib", "商保(亿珂/Imbruvica)"),
    ("泽布替尼", "zanubrutinib", "商保(百悦泽/Brukinsa)"),
    ("达雷妥尤单抗", "daratumumab", "商保(兆珂/Darzalex)"),
    # 可能不在 RxNorm 的国产创新药（预期 resolve 失败，作为边界演示）
    ("卡瑞利珠单抗", "camrelizumab", "商保(艾瑞卡)"),
    ("信迪利单抗", "sintilimab", "商保(达伯舒)"),
    ("替雷利珠单抗", "tislelizumab", "商保(百泽安)"),
]

# TTY 优先级：成分 > 临床药 > 精确成分 > 品牌
TTY_PRIO = "CASE Tty WHEN 'IN' THEN 1 WHEN 'SCD' THEN 2 WHEN 'PIN' THEN 3 WHEN 'BN' THEN 4 ELSE 9 END"


def resolve(cur, en):
    cur.execute(
        "SELECT TOP 1 Rxcui, Tty, Str FROM Terminology_Rxnorm.Concept "
        "WHERE Str = ? AND Tty IN ('IN','SCD','PIN','BN') ORDER BY " + TTY_PRIO, (en,))
    r = cur.fetchone()
    return (r[0], r[1], r[2]) if r else (None, None, None)


def main():
    conn = iris.dbapi.connect(hostname=HOST, port=PORT, namespace=NS,
                              username="superuser", password="SYS")
    cur = conn.cursor()
    cur.execute("DELETE FROM Terminology_Rxnorm.ChineseAlias")
    conn.commit()

    hit, miss = 0, []
    for zh, en, src in CANDIDATES:
        rxcui, tty, name = resolve(cur, en)
        if rxcui:
            cur.execute(
                "INSERT INTO Terminology_Rxnorm.ChineseAlias (ZhName, Rxcui, EnName, Tty, Source) "
                "VALUES (?, ?, ?, ?, ?)", (zh, rxcui, name or en, tty or "", src))
            hit += 1
        else:
            miss.append((zh, en))
    conn.commit()
    print(f"映射表写入 {hit} 条；未命中 {len(miss)} 条：")
    for zh, en in miss:
        print(f"  - {zh}（{en}）—— RxNorm 无对应")
    conn.close()


if __name__ == "__main__":
    main()
