"""Trove F2 — Aenderungen ueber alle Orte feststellen: neu / weg / verschoben / aussen geaendert zwischen zwei Laeufen.

Jeder Lauf nimmt einen Stand eines Ortes (Ordner) auf (Tabelle stand im Mandanten-Register) und vergleicht ihn mit
dem vorigen Stand desselben Ortes:
  neu              Pfad jetzt da, vorher nicht (und sein Inhalt war vorher an keinem anderen Pfad dieses Ortes)
  weg              Pfad vorher da, jetzt nicht (und sein Inhalt liegt jetzt an keinem neuen Pfad)
  verschoben       Inhalt (sha256) vorher unter Pfad A, jetzt unter Pfad B (A weg, B neu)
  aussen geaendert Pfad in beiden Staenden, Inhalt anders — und das Belegprotokoll kennt keine Trove-Bewegung dorthin
  durch trove      wie oben, aber das Belegprotokoll hat die neue Pruefsumme als kopiert/verschoben/verifiziert
Jeder Befund wird ins Belegprotokoll (F8) geschrieben. Nichts wird veraendert, nur gelesen und gerechnet.
Pruefsummen werden nur fuer neue oder veraenderte Dateien (Groesse/mtime) neu gerechnet.
"""
import argparse, hashlib, json, os, sqlite3, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import belegprotokoll as bp

BLOCK = 1 << 20


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(BLOCK), b""):
            h.update(b)
    return h.hexdigest()


def register_pfad(mandant):
    return os.path.join(bp.WURZEL, mandant, "register.db")


def _con(mandant):
    os.makedirs(os.path.dirname(register_pfad(mandant)), exist_ok=True)
    con = sqlite3.connect(register_pfad(mandant))
    con.execute("CREATE TABLE IF NOT EXISTS datei(ort TEXT,pfad TEXT,name TEXT,groesse INTEGER,mtime REAL,sha256 TEXT)")
    con.execute("CREATE INDEX IF NOT EXISTS i_sha ON datei(sha256)")
    con.execute("CREATE TABLE IF NOT EXISTS lauf(id INTEGER PRIMARY KEY, ort TEXT, wurzel TEXT, zeit TEXT, dateien INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS stand(lauf INTEGER, pfad TEXT, groesse INTEGER, mtime REAL, sha256 TEXT)")
    con.execute("CREATE INDEX IF NOT EXISTS i_stand ON stand(lauf, pfad)")
    return con


def aufnehmen(wurzel, con, skip_punkt=True):
    """Aktuellen Stand lesen; sha nur neu rechnen, wenn Groesse/mtime nicht mehr zum letzten bekannten Wert passen."""
    bekannt = {}
    for pfad, groesse, mtime, sha in con.execute("select pfad, groesse, mtime, sha256 from stand where pfad like ? order by lauf", (wurzel + "%",)):
        bekannt[pfad] = (groesse, mtime, sha)
    for pfad, groesse, mtime, sha in con.execute("select pfad, groesse, mtime, sha256 from datei where pfad like ?", (wurzel + "%",)):
        bekannt.setdefault(pfad, (groesse, mtime, sha))
    stand = {}
    gerechnet = 0
    for w, dirs, files in os.walk(wurzel):
        if skip_punkt:
            dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            p = os.path.join(w, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            alt = bekannt.get(p)
            if alt and alt[0] == st.st_size and abs((alt[1] or 0) - st.st_mtime) < 0.01 and alt[2]:
                sha = alt[2]
            else:
                try:
                    sha = _sha(p); gerechnet += 1
                except OSError:
                    continue
            stand[p] = (st.st_size, st.st_mtime, sha)
    return stand, gerechnet


def trove_kennt(protokoll_pfad, ziel, sha):
    """Hat Trove selbst (kopiert/verschoben/verifiziert) diesen Inhalt an diesen Pfad gebracht?"""
    if not os.path.exists(protokoll_pfad):
        return False
    z = bp._norm(ziel)
    for e in bp.eintraege(protokoll_pfad):
        if e.get("ereignis") in ("kopiert", "verschoben", "verifiziert") and e.get("sha256") == sha and bp._norm(e.get("ziel")) == z:
            return True
    return False


def lauf(mandant, ort, wurzel, werkzeug="aenderungen.py", nur_bericht=False):
    wurzel = os.path.abspath(wurzel)
    con = _con(mandant)
    vorher_id = con.execute("select max(id) from lauf where ort=?", (ort,)).fetchone()[0]
    vorher = {}
    if vorher_id:
        for pfad, groesse, mtime, sha in con.execute("select pfad, groesse, mtime, sha256 from stand where lauf=?", (vorher_id,)):
            vorher[pfad] = (groesse, mtime, sha)
    jetzt, gerechnet = aufnehmen(wurzel, con)
    sha_vorher = {}
    for p, (g, m, s) in vorher.items():
        sha_vorher.setdefault(s, []).append(p)
    sha_jetzt = {}
    for p, (g, m, s) in jetzt.items():
        sha_jetzt.setdefault(s, []).append(p)
    befunde = {"neu": [], "weg": [], "verschoben": [], "aussen_geaendert": [], "durch_trove": [], "unveraendert": 0}
    weg_pfade = [p for p in vorher if p not in jetzt]
    neu_pfade = [p for p in jetzt if p not in vorher]
    verwendet = set()
    for p in weg_pfade:
        s = vorher[p][2]
        kandidaten = [q for q in sha_jetzt.get(s, []) if q in neu_pfade and q not in verwendet]
        if kandidaten:
            q = kandidaten[0]; verwendet.add(q)
            befunde["verschoben"].append({"von": p, "nach": q, "sha256": s})
        else:
            befunde["weg"].append({"pfad": p, "sha256": s, "noch_woanders": [q for q in sha_jetzt.get(s, []) if q != p]})
    for q in neu_pfade:
        if q not in verwendet:
            befunde["neu"].append({"pfad": q, "sha256": jetzt[q][2], "auch_unter": [x for x in sha_jetzt.get(jetzt[q][2], []) if x != q]})
    prot_pfad = bp.pfad_fuer(mandant)
    for p in jetzt:
        if p in vorher:
            if vorher[p][2] == jetzt[p][2]:
                befunde["unveraendert"] += 1
            else:
                eintrag = {"pfad": p, "sha_vorher": vorher[p][2], "sha_jetzt": jetzt[p][2], "bytes_vorher": vorher[p][0], "bytes_jetzt": jetzt[p][0]}
                (befunde["durch_trove"] if trove_kennt(prot_pfad, p, jetzt[p][2]) else befunde["aussen_geaendert"]).append(eintrag)
    zeit = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    if nur_bericht:
        con.close()
        return {"ort": ort, "wurzel": wurzel, "erster_lauf": vorher_id is None, "dateien": len(jetzt), "gerechnet": gerechnet, "befunde": befunde}
    cur = con.execute("insert into lauf(ort, wurzel, zeit, dateien) values(?,?,?,?)", (ort, wurzel, zeit, len(jetzt)))
    lauf_id = cur.lastrowid
    con.executemany("insert into stand values(?,?,?,?,?)", [(lauf_id, p, g, m, s) for p, (g, m, s) in jetzt.items()])
    con.execute("delete from datei where ort=?", (ort,))
    con.executemany("insert into datei values(?,?,?,?,?,?)", [(ort, p, os.path.basename(p), g, m, s) for p, (g, m, s) in jetzt.items()])
    con.commit(); con.close()
    kennung = "f2-%s-%d" % (ort, lauf_id)
    with bp.Protokoll(prot_pfad) as prot:
        prot.schreiben("notiz", werkzeug=werkzeug, mandant=mandant, lauf=kennung, meldung="F2-Lauf %d beginnt: Ort %s = %s, %d Dateien, %d Pruefsummen neu gerechnet%s" % (lauf_id, ort, wurzel, len(jetzt), gerechnet, ", erster Lauf (Grundstand)" if vorher_id is None else ""))
        if vorher_id is not None:
            for b in befunde["neu"]:
                prot.schreiben("gesehen", quelle=b["pfad"], sha256=b["sha256"], bytes_=jetzt[b["pfad"]][0], werkzeug=werkzeug, mandant=mandant, lauf=kennung, meldung="neu seit letztem Lauf" + (", gleicher Inhalt auch unter %s" % b["auch_unter"] if b["auch_unter"] else ""), f2="neu")
            for b in befunde["weg"]:
                prot.schreiben("notiz", quelle=b["pfad"], sha256=b["sha256"], werkzeug=werkzeug, mandant=mandant, lauf=kennung, meldung="weg seit letztem Lauf" + (", Inhalt noch unter %s" % b["noch_woanders"] if b["noch_woanders"] else " — Inhalt an diesem Ort nicht mehr vorhanden"), f2="weg")
            for b in befunde["verschoben"]:
                prot.schreiben("verschoben", quelle=b["von"], ziel=b["nach"], sha256=b["sha256"], bytes_=jetzt[b["nach"]][0], werkzeug=werkzeug, mandant=mandant, lauf=kennung, meldung="ausserhalb von Trove verschoben/umbenannt, durch F2 erkannt", f2="verschoben")
            for b in befunde["aussen_geaendert"]:
                prot.schreiben("notiz", quelle=b["pfad"], ziel=b["pfad"], sha256=b["sha_jetzt"], bytes_=b["bytes_jetzt"], werkzeug=werkzeug, mandant=mandant, lauf=kennung, meldung="aussen geaendert: sha %s -> %s" % (b["sha_vorher"][:12], b["sha_jetzt"][:12]), sha_vorher=b["sha_vorher"], f2="aussen_geaendert")
            for b in befunde["durch_trove"]:
                prot.schreiben("notiz", quelle=b["pfad"], ziel=b["pfad"], sha256=b["sha_jetzt"], bytes_=b["bytes_jetzt"], werkzeug=werkzeug, mandant=mandant, lauf=kennung, meldung="Inhalt geaendert, Bewegung durch Trove belegt", sha_vorher=b["sha_vorher"], f2="durch_trove")
        prot.schreiben("notiz", werkzeug=werkzeug, mandant=mandant, lauf=kennung, meldung="F2-Lauf %d beendet: neu %d, weg %d, verschoben %d, aussen geaendert %d, durch Trove %d, unveraendert %d" % (lauf_id, len(befunde["neu"]), len(befunde["weg"]), len(befunde["verschoben"]), len(befunde["aussen_geaendert"]), len(befunde["durch_trove"]), befunde["unveraendert"]))
        prot.anker_setzen(mandant, kennung)
    return {"ort": ort, "wurzel": wurzel, "lauf": lauf_id, "erster_lauf": vorher_id is None, "dateien": len(jetzt), "gerechnet": gerechnet, "befunde": befunde}


def main():
    ap = argparse.ArgumentParser(description="Trove F2: Aenderungen zwischen zwei Laeufen feststellen")
    ap.add_argument("--mandant", default="haus")
    ap.add_argument("--ort", required=True, help="Kennung des Ortes, z. B. opos")
    ap.add_argument("--wurzel", required=True, help="Ordner")
    ap.add_argument("--nur-bericht", action="store_true", help="nur vergleichen, keinen Stand speichern, nichts protokollieren")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = lauf(a.mandant, a.ort, a.wurzel, nur_bericht=a.nur_bericht)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1)); return
    b = r["befunde"]
    print("Ort %s (%s): Lauf %s, %d Dateien, %d Pruefsummen gerechnet%s" % (r["ort"], r["wurzel"], r.get("lauf", "-"), r["dateien"], r["gerechnet"], " — erster Lauf, Grundstand aufgenommen" if r["erster_lauf"] else ""))
    for k in ("neu", "weg", "verschoben", "aussen_geaendert", "durch_trove"):
        print("  %-17s %d" % (k, len(b[k])))
        for x in b[k][:20]:
            print("     ", x.get("pfad") or ("%s -> %s" % (x.get("von"), x.get("nach"))), ("sha %s->%s" % (x["sha_vorher"][:8], x["sha_jetzt"][:8])) if "sha_vorher" in x else "")
    print("  unveraendert      %d" % b["unveraendert"])


if __name__ == "__main__":
    main()
