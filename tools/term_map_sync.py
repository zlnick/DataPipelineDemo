#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 术语转换映射种子：export（服务器 → data/seeds/term_map_seed.json）/ import（种子 → 服务器，幂等）
# 用法: python3 tools/term_map_sync.py export|import [--check] [--dry] [--wait 120]
import argparse, base64, json, os, sys, time, urllib.request
SEED = "data/seeds/term_map_seed.json"
AUTH = {"Authorization": "Basic " + base64.b64encode(b"superuser:SYS").decode(), "Accept": "application/json"}

def req(url, payload=None, timeout=60):
    h = dict(AUTH); data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json; charset=utf-8"
    r = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))

def wait(base, secs):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            req(base + "/mapping/systems", timeout=5); return True
        except Exception:
            time.sleep(5)
    return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["export", "import"])
    ap.add_argument("--url", default="http://localhost:52774/terminology")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--wait", type=int, default=0)
    a = ap.parse_args()
    base = a.url.rstrip("/")
    if a.wait and not wait(base, a.wait):
        print("术语服务器未就绪:", base, file=sys.stderr); return 2
    try:

        if a.action == "export":
            items = req(base + "/mapping/entries?limit=1000").get("items") or []
            print("服务器映射 %d 条" % len(items))
            if a.check:
                return 0
            if not items:
                print("服务器无映射，拒绝写空种子", file=sys.stderr)
                return 1
            os.makedirs("data/seeds", exist_ok=True)
            with open(SEED, "w", encoding="utf-8") as fh:
                json.dump({"count": len(items), "items": items}, fh, ensure_ascii=False, indent=1)
            print("已写出", SEED)
            return 0

        with open(SEED, encoding="utf-8") as fh:
            items = (json.load(fh).get("items") or [])
        print("种子 %d 条" % len(items))
        if a.dry:
            print(json.dumps(items[:2], ensure_ascii=False)[:300])
            return 0
        ins = upd = fail = 0

        for i in range(0, len(items), 100):
            r = req(base + "/mapping/entries", {("items"): [{(k[:1].lower() + k[1:]): v for k, v in x.items()} for x in items[i:i + 100]]})
            ins += int(r.get("inserted") or 0)
            upd += int(r.get("updated") or 0)
            fail += len(r.get("failed") or [])
        print("导入完成: inserted=%d updated=%d failed=%d" % (ins, upd, fail))
        return 0 if fail == 0 else 1

    except Exception as exc:
        print("失败:", exc, file=sys.stderr)
        return 2
if __name__ == "__main__":
    raise SystemExit(main())
