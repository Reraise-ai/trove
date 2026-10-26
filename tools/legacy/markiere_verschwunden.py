import json, sys, time
prot, status = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "quelle_verschwunden")
f = open(prot, "a", encoding="utf-8", buffering=1)
n = 0
for l in sys.stdin:
    q = l.strip()
    if not q:
        continue
    f.write(json.dumps({"zeit": time.strftime("%Y-%m-%dT%H:%M:%S"), "quelle": q, "status": status}, ensure_ascii=False) + "\n")
    n += 1
f.close(); print("markiert", n, status)
