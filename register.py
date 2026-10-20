#!/usr/bin/env python3
"""F1 Register (Datei-Zentrale). Nur lesend: Fingerabdruecke -> SQLite,
meldet Dubletten und 'liegt nur hier'. Veraendert/verschiebt/loescht nie.
--endungen begrenzt auf Dateitypen, --min-bytes nimmt zusaetzlich grosse Dateien
(z.B. Modellgewichte), --skip-dirs prunt Programmordner."""
import argparse, hashlib, os, sqlite3, sys, time, json

def sha256(path, buf=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(buf), b""):
            h.update(chunk)
    return h.hexdigest()

def scan(root, label, con, db_abs, endungen, skipdirs, min_bytes):
    con.execute("DELETE FROM datei WHERE ort = ?", (label,))
    n = 0
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in skipdirs]
        for name in names:
            if name.startswith("."):
                continue
            p = os.path.join(dirpath, name)
            if os.path.abspath(p) == db_abs:
                continue
            try:
                st = os.stat(p)
            except (OSError, PermissionError) as e:
                con.execute("INSERT INTO fehler(ort,pfad,grund) VALUES (?,?,?)", (label, p, str(e)))
                continue
            inc = endungen is None
            if endungen is not None:
                ext = os.path.splitext(name)[1].lower().lstrip(".")
                if ext in endungen:
                    inc = True
            if min_bytes and st.st_size >= min_bytes:
                inc = True
            if not inc:
                continue
            try:
                digest = sha256(p)
            except (OSError, PermissionError) as e:
                con.execute("INSERT INTO fehler(ort,pfad,grund) VALUES (?,?,?)", (label, p, str(e)))
                continue
            con.execute("INSERT INTO datei(ort,pfad,name,groesse,mtime,sha256) VALUES (?,?,?,?,?,?)",
                        (label, p, name, st.st_size, st.st_mtime, digest))
            n += 1
            if n % 200 == 0:
                con.commit()
    con.commit()
    return n

def bericht(con):
    cur = con.cursor()
    gesamt = cur.execute("SELECT COUNT(*), COALESCE(SUM(groesse),0) FROM datei").fetchone()
    dub = cur.execute("SELECT sha256, COUNT(*) c FROM datei GROUP BY sha256 HAVING c>1").fetchall()
    nur_hier = cur.execute(
        "SELECT ort, COUNT(*), COALESCE(SUM(groesse),0) FROM datei WHERE sha256 IN "
        "(SELECT sha256 FROM datei GROUP BY sha256 HAVING COUNT(DISTINCT ort)=1) GROUP BY ort").fetchall()
    fehler = cur.execute("SELECT COUNT(*) FROM fehler").fetchone()[0]
    return {"dateien": gesamt[0], "bytes": gesamt[1],
            "dubletten_gruppen": len(dub), "dubletten_dateien": sum(c for _, c in dub),
            "nur_hier_je_ort": {o: {"dateien": n, "bytes": b} for o, n, b in nur_hier},
            "lesefehler": fehler}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+", help="label=pfad")
    ap.add_argument("--db", default="register.db")
    ap.add_argument("--endungen", default=None)
    ap.add_argument("--skip-dirs", default="")
    ap.add_argument("--min-bytes", type=int, default=0)
    args = ap.parse_args()
    db_abs = os.path.abspath(args.db)
    endungen = set(e.strip().lower() for e in args.endungen.split(",")) if args.endungen else None
    skipdirs = set(d.strip() for d in args.skip_dirs.split(",") if d.strip())
    con = sqlite3.connect(args.db)
    con.executescript(
        "CREATE TABLE IF NOT EXISTS datei(ort TEXT,pfad TEXT,name TEXT,groesse INTEGER,mtime REAL,sha256 TEXT);"
        "CREATE TABLE IF NOT EXISTS fehler(ort TEXT,pfad TEXT,grund TEXT);"
        "CREATE INDEX IF NOT EXISTS i_sha ON datei(sha256);")
    con.execute("DELETE FROM fehler")
    t0 = time.time()
    for r in args.roots:
        label, _, pfad = r.partition("=")
        if not pfad:
            print(f"uebersprungen: {r}", file=sys.stderr); continue
        n = scan(pfad, label, con, db_abs, endungen, skipdirs, args.min_bytes)
        print(f"{label}: {n} Dateien ({pfad})", file=sys.stderr)
    b = bericht(con); b["sekunden"] = round(time.time() - t0, 2)
    print(json.dumps(b, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
