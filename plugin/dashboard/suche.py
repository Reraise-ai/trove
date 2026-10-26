"""Trove search over what Trove itself recorded -- "where does this file live?". Local only, no model, no network.

Sources:
  register   <TROVE_DATEN>/<client>/register.db (table datei: ort, pfad, name, sha256) -- what Copy/Move/Sync registered
  protokoll  receipt log (paths, sha256, messages -- read results, copies, moves)
Search by file name, path fragment, or (full or 8+ char prefix of) sha256.
"""
import argparse, json, os, sqlite3, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import belegprotokoll as bp


def _like(q):
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def register(mandant, q, n=30):
    out = []
    db = os.path.join(bp.WURZEL, mandant, "register.db")
    if not os.path.exists(db):
        return out
    c = sqlite3.connect(db)
    try:
        for ort, pfad, name, groesse, sha in c.execute(
                "select ort,pfad,name,groesse,sha256 from datei where name like ? escape '\\' or pfad like ? escape '\\' or sha256 = ? limit ?",
                (_like(q), _like(q), q.lower(), n)):
            voll = pfad if os.path.isabs(pfad or "") else os.path.join(ort or "", pfad or "")
            out.append({"quelle": "register", "ort": ort, "pfad": voll, "name": name, "bytes": groesse, "sha256": sha})
    finally:
        c.close()
    return out


def protokoll(mandant, q, n=40):
    ql = q.casefold()
    out = []
    if not os.path.exists(bp.pfad_fuer(mandant)):
        return out
    for e in bp.eintraege(bp.pfad_fuer(mandant)):
        if ql in (e.get("quelle") or "").casefold() or ql in (e.get("ziel") or "").casefold() or ql in (e.get("meldung") or "").casefold() or (len(q) >= 8 and (e.get("sha256") or "").startswith(ql)):
            out.append({"nr": e["nr"], "zeit": e["zeit"], "ereignis": e["ereignis"], "quelle": e.get("quelle"), "ziel": e.get("ziel"),
                        "sha256": e.get("sha256"), "meldung": (e.get("meldung") or "")[:200]})
    return out[-n:][::-1]


def suchen(q, mandant="haus", n=30):
    q = (q or "").strip()
    if len(q) < 2:
        return {"frage": q, "hinweis": "at least 2 characters"}
    reg = register(mandant, q, n)
    prot = protokoll(mandant, q, n)
    orte = {r["pfad"] for r in reg}
    for p in prot:
        for k in ("quelle", "ziel"):
            if p.get(k) and q.casefold() in p[k].casefold():
                orte.add(p[k])
    return {"frage": q, "mandant": mandant, "liegt": sorted(orte)[:n], "register": reg, "protokoll": prot}


def main():
    ap = argparse.ArgumentParser(description="Trove search over the register and the receipt log")
    ap.add_argument("frage")
    ap.add_argument("--mandant", default="haus")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = suchen(a.frage, a.mandant)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return
    print("found at:")
    for p in r.get("liegt", []):
        print("   ", p)
    print("register %d, receipt log %d" % (len(r.get("register", [])), len(r.get("protokoll", []))))


if __name__ == "__main__":
    main()
