#!/usr/bin/env python3
import os, sqlite3, json, sys
DB = sys.argv[1] if len(sys.argv) > 1 else os.environ['TROVE_REGISTER_DB']
con = sqlite3.connect(DB)
print("tables:", [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()])
print("schema datei:", con.execute("SELECT sql FROM sqlite_master WHERE name='datei'").fetchone())
print("sample datei:")
for r in con.execute("SELECT * FROM datei LIMIT 5"):
    print(r)
print("--- orte ---")
for r in con.execute("SELECT ort, COUNT(*), COALESCE(SUM(groesse),0) FROM datei GROUP BY ort").fetchall():
    print(r)
print("--- bookkeeping folder search ---")
for r in con.execute("SELECT pfad,name,groesse,sha256 FROM datei WHERE pfad LIKE '%Buchhaltung%' OR pfad LIKE '%Eingangsrechnungen%' OR pfad LIKE '%nextcloud%' LIMIT 30").fetchall():
    print(r)
print("--- folder prefixes (top levels) ---")
for r in con.execute("SELECT DISTINCT substr(pfad,1,80) FROM datei LIMIT 40").fetchall():
    print(r)
