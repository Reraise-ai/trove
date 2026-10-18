import json, sqlite3, sys
db, prot = sys.argv[1], sys.argv[2]
idx = set(r[0] for r in sqlite3.connect(db).execute("select pfad from datei where ort='zarmina'"))
kopiert, erklaert, err = set(), set(), set()
for line in open(prot, encoding="utf-8"):
    e = json.loads(line); s = e["status"]; q = e["quelle"]
    if s in ("ok", "geaendert", "schon_da"):
        kopiert.add(q); erklaert.add(q)
    elif s in ("quelle_verschwunden", "symlink"):
        erklaert.add(q)
    elif s == "fehler":
        err.add(q)
offen = idx - erklaert
print("index_eindeutig", len(idx))
print("kopiert+verifiziert", len(kopiert & idx))
print("erklaert_nicht_kopiert(verschwunden/symlink)", len((erklaert - kopiert) & idx))
print("abgedeckt_gesamt", len(erklaert & idx))
print("NOCH_OFFEN", len(offen))
print("echte_fehler_offen", len(err - erklaert))
for p in list(offen)[:8]:
    print("OFFEN:", p)
