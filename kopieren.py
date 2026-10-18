"""Trove: verifizierter Empfang eines tar-Streams (Push von zarmina oder einer anderen Quelle) auf den Server.

Regeln (Plan §1.2/§6, Review 18.10.):
- Nichts wird ueberschrieben. Liegt am Ziel eine Datei mit ANDEREM Inhalt, wird nicht geschrieben, sondern ein
  Konflikt protokolliert (ereignis fehler, status konflikt, sha_vorher).
- Jede Datei wird chunkweise in <ziel>.teil geschrieben und dabei gehasht, dann atomar umbenannt und danach
  von der Platte zurueckgelesen: erst dann "verifiziert". "kopiert" allein heisst: Stream-Pruefsumme stimmt.
- Namenskollisionen nach Windows-Kodierung (a:b vs a%3Ab, foo. vs foo, A.pdf vs a.pdf) werden als Konflikt
  erkannt, nicht still ueberschrieben.
- Jeder Lauf hat eine Start- und eine Ende-Zeile im Belegprotokoll (auch bei Abbruch).
"""
import argparse, hashlib, json, os, sqlite3, sys, tarfile, time
import belegprotokoll

BAD = set('<>:"|?*%')
RESERVIERT = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}
BLOCK = 1 << 20


def sha_datei(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(BLOCK), b""):
            h.update(block)
    return h.hexdigest()


def safe_seg(seg):
    out = ''.join('%%%02X' % ord(c) if (c in BAD or ord(c) < 32) else c for c in seg)
    out = out.rstrip(' .')
    if out.split('.')[0].upper() in RESERVIERT:
        out = '_' + out
    return out or '_'


def win_join(wurzel, posix_name):
    parts = [safe_seg(p) for p in posix_name.split('/') if p]
    return os.path.join(wurzel, *parts)


def ext(p):
    if os.name != 'nt':
        return p
    p = os.path.abspath(p)
    if p.startswith('\\\\?\\'):
        return p
    if p.startswith('\\\\'):
        return '\\\\?\\UNC\\' + p[2:]
    return '\\\\?\\' + p


def main():
    ap = argparse.ArgumentParser(description="Trove: verifizierter Empfang eines tar-Streams (Push).")
    ap.add_argument("--db", required=True, help="Register (SQLite) der Quelle; sha256 je Pfad")
    ap.add_argument("--quell-prefix", default="/", help="Praefix, das den tar-Namen vorangestellt wird, um den Indexschluessel zu bilden")
    ap.add_argument("--ziel", required=True)
    ap.add_argument("--protokoll", required=True, help="F8-Belegprotokoll (JSONL, append-only)")
    ap.add_argument("--mandant", default="haus")
    ap.add_argument("--lauf", default="")
    a = ap.parse_args()
    a.ziel = os.path.abspath(a.ziel)
    lauf = a.lauf or ("push-" + time.strftime("%Y%m%d-%H%M%S"))

    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)

    con = sqlite3.connect(a.db)
    os.makedirs(a.ziel, exist_ok=True)
    prot = belegprotokoll.Protokoll(a.protokoll)
    stat = {"gesamt": 0, "ok": 0, "schon_da": 0, "geaendert": 0, "nicht_im_index": 0, "konflikt": 0, "fehler": 0}
    ziele_im_lauf = {}
    abbruch = None
    prot.schreiben("notiz", werkzeug="kopieren.py", mandant=a.mandant, lauf=lauf, meldung="Lauf beginnt: Ziel " + a.ziel + ", Index " + a.db)
    try:
        tar = tarfile.open(fileobj=sys.stdin.buffer, mode="r|")
        for m in tar:
            if not m.isfile():
                continue
            stat["gesamt"] += 1
            indexkey = a.quell_prefix.rstrip("/") + "/" + m.name
            ziel = win_join(a.ziel, m.name)
            kodiert = (ziel != os.path.join(a.ziel, *[p for p in m.name.split('/') if p]))
            e = {"quelle": indexkey, "ziel": ziel, "bytes": m.size, "sha_kopie": "", "sha_index": None, "meldung": ""}
            tmp = ziel + ".teil"
            try:
                row = con.execute("select sha256 from datei where pfad=?", (indexkey,)).fetchone()
                e["sha_index"] = row[0] if row else None
                schluessel = ziel.casefold()
                if schluessel in ziele_im_lauf and ziele_im_lauf[schluessel] != m.name:
                    raise RuntimeError("konflikt: Zielname kollidiert mit %r aus diesem Lauf" % ziele_im_lauf[schluessel])
                ziele_im_lauf[schluessel] = m.name
                # Stream chunkweise in .teil schreiben und dabei hashen
                d = os.path.dirname(ziel)
                if d:
                    os.makedirs(ext(d), exist_ok=True)
                h = hashlib.sha256()
                src = tar.extractfile(m)
                with open(ext(tmp), "wb") as f:
                    for block in iter(lambda: src.read(BLOCK), b""):
                        h.update(block); f.write(block)
                    f.flush(); os.fsync(f.fileno())
                sha = h.hexdigest(); e["sha_kopie"] = sha
                if os.path.exists(ext(ziel)):
                    vorher = sha_datei(ext(ziel))
                    if vorher == sha:
                        os.remove(ext(tmp))
                        e["status"] = "schon_da"; stat["schon_da"] += 1
                        prot.schreiben("gesehen", quelle=indexkey, ziel=ziel, sha256=sha, bytes_=m.size, werkzeug="kopieren.py",
                                       mandant=a.mandant, lauf=lauf, status_alt="schon_da", sha_index=e["sha_index"], name_kodiert=kodiert)
                        continue
                    os.remove(ext(tmp))
                    e["status"] = "konflikt"; stat["konflikt"] += 1
                    prot.schreiben("fehler", quelle=indexkey, ziel=ziel, sha256=sha, bytes_=m.size, werkzeug="kopieren.py",
                                   mandant=a.mandant, lauf=lauf, meldung="konflikt: Ziel vorhanden mit anderem Inhalt, nicht ueberschrieben",
                                   status_alt="konflikt", sha_vorher=vorher, sha_index=e["sha_index"], name_kodiert=kodiert)
                    continue
                os.replace(ext(tmp), ext(ziel))
                try:
                    os.utime(ext(ziel), (m.mtime, m.mtime))
                except OSError:
                    pass
                if row is None:
                    st = "nicht_im_index"
                elif row[0] != sha:
                    st = "geaendert"
                else:
                    st = "ok"
                stat[st] += 1
                prot.schreiben("kopiert", quelle=indexkey, ziel=ziel, sha256=sha, bytes_=m.size, werkzeug="kopieren.py",
                               mandant=a.mandant, lauf=lauf, meldung=(st if st != "ok" else ""), status_alt=st,
                               sha_index=e["sha_index"], name_kodiert=kodiert)
                # Gegenprobe von der Platte
                zurueck = sha_datei(ext(ziel))
                if zurueck == sha:
                    prot.schreiben("verifiziert", quelle=ziel, ziel=ziel, sha256=sha, bytes_=m.size, werkzeug="kopieren.py",
                                   mandant=a.mandant, lauf=lauf, meldung="von der Platte zurueckgelesen, sha gleich")
                else:
                    stat["fehler"] += 1
                    prot.schreiben("fehler", quelle=ziel, ziel=ziel, sha256=zurueck, bytes_=m.size, werkzeug="kopieren.py",
                                   mandant=a.mandant, lauf=lauf, meldung="Gegenprobe fehlgeschlagen: Platte %s != Stream %s" % (zurueck[:16], sha[:16]))
            except Exception as ex:
                try:
                    if os.path.exists(ext(tmp)):
                        os.remove(ext(tmp))
                except OSError:
                    pass
                meldung = str(ex) or type(ex).__name__
                if meldung.startswith("konflikt"):
                    stat["konflikt"] += 1; st_alt = "konflikt"
                else:
                    stat["fehler"] += 1; st_alt = "fehler"
                prot.schreiben("fehler", quelle=indexkey, ziel=ziel, sha256=e["sha_kopie"], bytes_=m.size, werkzeug="kopieren.py",
                               mandant=a.mandant, lauf=lauf, meldung=meldung, status_alt=st_alt, sha_index=e["sha_index"], name_kodiert=kodiert)
    except BaseException as ex:  # Stream abgebrochen, Ctrl-C, Protokollfehler
        abbruch = "%s: %s" % (type(ex).__name__, ex)
        raise
    finally:
        try:
            prot.schreiben("notiz", werkzeug="kopieren.py", mandant=a.mandant, lauf=lauf,
                           meldung=("Lauf abgebrochen: " + abbruch if abbruch else "Lauf beendet") + " " + json.dumps(stat, ensure_ascii=False))
            prot.anker_setzen(a.mandant, lauf)
        finally:
            prot.close(); con.close()
    print(json.dumps(stat, ensure_ascii=False))
    sys.exit(0 if stat["fehler"] == 0 else 3)


if __name__ == "__main__":
    main()
