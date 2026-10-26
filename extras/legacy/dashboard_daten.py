import json, sys, collections
prot = sys.argv[1]
prefix = sys.argv[2] if len(sys.argv) > 2 else ""
final = {}  # quelle -> letzter Eintrag (finaler Status gewinnt)
for line in open(prot, encoding="utf-8"):
    e = json.loads(line); final[e["quelle"]] = e
blocks = collections.defaultdict(lambda: {"n": 0, "bytes": 0, "st": collections.Counter()})
tot = {"n": 0, "bytes": 0, "st": collections.Counter()}
for q, e in final.items():
    key = "/".join(q.strip("/").split("/")[:3]) or q
    b = blocks[key]; b["n"] += 1; b["bytes"] += e.get("bytes") or 0; b["st"][e["status"]] += 1
    tot["n"] += 1; tot["bytes"] += e.get("bytes") or 0; tot["st"][e["status"]] += 1
top = sorted(blocks.items(), key=lambda kv: -kv[1]["bytes"])[:14]
out = {"total": {"n": tot["n"], "gb": round(tot["bytes"]/1e9, 2), "st": dict(tot["st"])},
       "blocks": [{"k": k.replace(prefix, "") if prefix else k, "n": v["n"], "gb": round(v["bytes"]/1e9, 3), "st": dict(v["st"])} for k, v in top]}
print(json.dumps(out, ensure_ascii=False))
