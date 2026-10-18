#!/usr/bin/env python3
import sqlite3, json
DB = r'C:\datenlager\_eingang\datei-zentrale\reg_z_2026-10-17.db'
con = sqlite3.connect(DB)
print("tables:", [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()])
print("schema datei:", con.execute("SELECT sql FROM sqlite_master WHERE name='datei'").fetchone())
print("sample datei:")
for r in con.execute("SELECT * FROM datei LIMIT 5"):
    print(r)
print("--- orte ---")
for r in con.execute("SELECT ort, COUNT(*), COALESCE(SUM(groesse),0) FROM datei GROUP BY ort").fetchall():
    print(r)
print("--- progrow / buchhaltung folder search ---")
for r in con.execute("SELECT pfad,name,groesse,sha256 FROM datei WHERE pfad LIKE '%Buchhaltung%' OR pfad LIKE '%Progrow%' OR pfad LIKE '%Eingangsrechnungen%' OR pfad LIKE '%nextcloud%' LIMIT 30").fetchall():
    print(r)
print("--- folder prefixes (top levels) ---")
for r in con.execute("SELECT DISTINCT substr(pfad,1,80) FROM datei LIMIT 40").fetchall():
    print(r)
