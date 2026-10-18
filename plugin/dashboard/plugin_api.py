"""Trove — Backend des Hermes-Desktop-Tabs. Mount: /api/plugins/trove/

F9: Quellen einbinden (lokaler Ordner oder rclone-Remote), zwei Seiten (Liste + Vorschau), Aktionen
kopieren / verschieben / synchronisieren — jede Aktion laeuft ueber das Belegprotokoll (F8) mit Gegenprobe
(sha256 von der Platte zurueckgelesen). Dubletten ueber das Register (F1, SQLite je Mandant).
Nichts wird ueberschrieben; verschieben loescht die Quelle erst nach bestandener Gegenprobe und nur bei
lokalen Quellen (Welt A). rclone-Remotes sind nur lesend (Welt B: Spiegel statt Umzug).
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import time

from fastapi import APIRouter, HTTPException

TROVE_REPO = os.environ.get("TROVE_REPO", r"C:\Projects\trove")
TROVE_DATEN = os.environ.get("TROVE_DATEN", r"C:\datenlager\trove")
REGISTER_ALT = os.environ.get("TROVE_REGISTER_DB", r"C:\datenlager\_eingang\datei-zentrale\reg_z_2026-10-17.db")
RCLONE = os.environ.get("TROVE_RCLONE", os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\WinGet\Links\rclone.exe"))
if TROVE_REPO not in sys.path:
    sys.path.insert(0, TROVE_REPO)
import belegprotokoll as bp  # noqa: E402
import aenderungen  # noqa: E402

router = APIRouter()
BLOCK = 1 << 20
MANDANT = "haus"


def _jetzt():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _sha(pfad):
    h = hashlib.sha256()
    with open(pfad, "rb") as f:
        for b in iter(lambda: f.read(BLOCK), b""):
            h.update(b)
    return h.hexdigest()


def _mandant_pruefen(mandant):
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", mandant or ""):
        raise HTTPException(400, "mandant ungueltig (a-z, 0-9, _ -)")
    return mandant


def _mandant_dir(mandant):
    _mandant_pruefen(mandant)
    d = os.path.join(TROVE_DATEN, mandant)
    os.makedirs(d, exist_ok=True)
    return d


def _quellen_pfad(mandant):
    return os.path.join(_mandant_dir(mandant), "quellen.json")


def _quellen(mandant):
    p = _quellen_pfad(mandant)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return []


def _quellen_speichern(mandant, liste):
    p = _quellen_pfad(mandant)
    tmp = p + ".neu"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(liste, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _quelle(mandant, qid):
    for q in _quellen(mandant):
        if q["id"] == qid:
            return q
    raise HTTPException(404, "Quelle %s unbekannt" % qid)


def _rclone(*args, timeout=120):
    if not os.path.exists(RCLONE):
        raise HTTPException(500, "rclone nicht gefunden: " + RCLONE)
    r = subprocess.run([RCLONE, *args], capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise HTTPException(502, "rclone: " + (r.stderr.strip()[-400:] or "Fehler %d" % r.returncode))
    return r.stdout


def _sicher(wurzel, rel):
    """Relativen Pfad unter der Quellwurzel aufloesen; nie darueber hinaus."""
    rel = (rel or "").replace("\\", "/").strip("/")
    voll = os.path.normpath(os.path.join(wurzel, *[s for s in rel.split("/") if s and s not in (".", "..")]))
    if os.path.commonpath([os.path.normpath(wurzel), voll]) != os.path.normpath(wurzel):
        raise HTTPException(400, "Pfad ausserhalb der Quelle")
    return voll


# ---------- Register (F1) je Mandant: sha -> Fundorte ----------

def _register_pfad(mandant):
    return os.path.join(_mandant_dir(mandant), "register.db")


def _register_con(mandant):
    con = sqlite3.connect(_register_pfad(mandant))
    con.execute("CREATE TABLE IF NOT EXISTS datei(ort TEXT,pfad TEXT,name TEXT,groesse INTEGER,mtime REAL,sha256 TEXT)")
    con.execute("CREATE INDEX IF NOT EXISTS i_sha ON datei(sha256)")
    con.execute("CREATE INDEX IF NOT EXISTS i_pfad ON datei(ort,pfad)")
    return con


def _sha_bekannt(mandant, pfade):
    """pfad -> sha aus dem Register (falls mtime+groesse noch stimmen) — kein Neuhashen."""
    con = _register_con(mandant)
    out = {}
    for p, groesse, mtime in pfade:
        row = con.execute("select sha256, groesse, mtime from datei where pfad=? order by rowid desc limit 1", (p,)).fetchone()
        if row and row[1] == groesse and abs((row[2] or 0) - mtime) < 0.01:
            out[p] = row[0]
    con.close()
    return out


def _dubletten_zaehler(mandant, shas):
    if not shas:
        return {}
    con = _register_con(mandant)
    alt = None
    if os.path.exists(REGISTER_ALT):
        try:
            alt = sqlite3.connect("file:%s?mode=ro" % REGISTER_ALT.replace("\\", "/"), uri=True)
        except sqlite3.Error:
            alt = None
    out = {}
    for s in set(shas):
        n = con.execute("select count(distinct pfad) from datei where sha256=?", (s,)).fetchone()[0]
        if alt is not None:
            n += alt.execute("select count(*) from datei where sha256=?", (s,)).fetchone()[0]
        out[s] = n
    con.close()
    if alt is not None:
        alt.close()
    return out


# ---------- Routen ----------

@router.get("/status")
def status():
    mandanten = sorted(d for d in os.listdir(TROVE_DATEN) if os.path.isdir(os.path.join(TROVE_DATEN, d)) and not d.startswith("_")) if os.path.isdir(TROVE_DATEN) else []
    prot = []
    for m in mandanten:
        pf = bp.pfad_fuer(m)
        if os.path.exists(pf):
            try:
                letzte, _ = bp._letzte_zeile(pf)
            except bp.ProtokollDefekt as ex:
                prot.append({"mandant": m, "pfad": pf, "fehler": str(ex)}); continue
            prot.append({"mandant": m, "pfad": pf, "bytes": os.path.getsize(pf), "zeilen": letzte.get("nr") if letzte else 0,
                         "letzte_zeit": letzte.get("zeit") if letzte else None, "letztes_ereignis": letzte.get("ereignis") if letzte else None,
                         "kette_ende": (letzte.get("kette") or "")[:16] if letzte else None, "anker": bp.letzter_anker(m)})
    reg = {"pfad": REGISTER_ALT, "vorhanden": os.path.exists(REGISTER_ALT)}
    if reg["vorhanden"]:
        try:
            con = sqlite3.connect("file:%s?mode=ro" % REGISTER_ALT.replace("\\", "/"), uri=True)
            n, b = con.execute("select count(*), coalesce(sum(groesse),0) from datei").fetchone()
            reg.update({"dateien": n, "bytes": b, "dubletten_gruppen": con.execute("select count(*) from (select sha256 from datei group by sha256 having count(*)>1)").fetchone()[0]})
            con.close()
        except sqlite3.Error as ex:
            reg["fehler"] = str(ex)
    return {"ok": True, "zeit": _jetzt(), "stufe": "F2 F3 F4 F5 F6 F7 F8 F9 F10 F11 F15", "geraet": os.environ.get("COMPUTERNAME", ""),
            "pid": os.getpid(), "protokolle": prot, "register": reg, "rclone": os.path.exists(RCLONE)}


@router.get("/quellen")
def quellen(mandant: str = MANDANT):
    liste = _quellen(mandant)
    remotes = []
    if os.path.exists(RCLONE):
        try:
            remotes = [r.strip().rstrip(":") for r in _rclone("listremotes", timeout=20).splitlines() if r.strip()]
        except HTTPException:
            remotes = []
    return {"quellen": liste, "rclone_remotes": remotes}


@router.post("/quellen")
def quelle_anlegen(body: dict):
    mandant = body.get("mandant") or MANDANT
    art = body.get("art")
    name = (body.get("name") or "").strip()
    if art == "ordner":
        pfad = os.path.abspath(body.get("pfad") or "")
        if not os.path.isdir(pfad):
            raise HTTPException(400, "Ordner nicht gefunden: " + pfad)
        q = {"id": hashlib.sha1(("ordner:" + pfad.casefold()).encode()).hexdigest()[:10], "art": "ordner", "name": name or pfad, "pfad": pfad, "nur_lesen": bool(body.get("nur_lesen", False))}
    elif art == "rclone":
        remote = (body.get("remote") or "").rstrip(":")
        unter = (body.get("pfad") or "").strip("/")
        _rclone("lsjson", "%s:%s" % (remote, unter), "--max-depth", "1", timeout=60)  # Erreichbarkeit pruefen
        q = {"id": hashlib.sha1(("rclone:%s:%s" % (remote, unter)).casefold().encode()).hexdigest()[:10], "art": "rclone", "name": name or (remote + ":" + unter),
             "remote": remote, "pfad": unter, "nur_lesen": True}
    else:
        raise HTTPException(400, "art muss ordner oder rclone sein")
    liste = [x for x in _quellen(mandant) if x["id"] != q["id"]] + [q]
    _quellen_speichern(mandant, liste)
    bp.schreiben(bp.pfad_fuer(mandant), ereignis="notiz", werkzeug="trove-plugin", mandant=mandant,
                 meldung="Quelle eingebunden: %s (%s)" % (q["name"], q["art"]), quelle_id=q["id"], quelle_art=q["art"],
                 quelle_pfad=q.get("pfad", ""), quelle_remote=q.get("remote", ""))
    return q


@router.delete("/quellen/{qid}")
def quelle_entfernen(qid: str, mandant: str = MANDANT):
    q = _quelle(mandant, qid)
    _quellen_speichern(mandant, [x for x in _quellen(mandant) if x["id"] != qid])
    bp.schreiben(bp.pfad_fuer(mandant), ereignis="notiz", werkzeug="trove-plugin", mandant=mandant, meldung="Quelle entfernt (nur aus der Liste): " + q["name"], quelle_id=qid)
    return {"ok": True}


def _liste_ordner(q, rel, mandant):
    wurzel = q["pfad"]
    voll = _sicher(wurzel, rel)
    if not os.path.isdir(voll):
        raise HTTPException(404, "Ordner nicht gefunden")
    eintraege = []
    with os.scandir(voll) as it:
        for d in it:
            try:
                st = d.stat(follow_symlinks=False)
                ist_ordner = d.is_dir(follow_symlinks=False)
            except OSError:
                continue
            eintraege.append({"name": d.name, "ordner": ist_ordner, "groesse": 0 if ist_ordner else st.st_size, "mtime": st.st_mtime, "pfad": os.path.join(voll, d.name)})
    shas = _sha_bekannt(mandant, [(e["pfad"], e["groesse"], e["mtime"]) for e in eintraege if not e["ordner"]])
    zaehler = _dubletten_zaehler(mandant, list(shas.values()))
    for e in eintraege:
        s = shas.get(e["pfad"])
        e["sha256"] = s
        e["fundorte"] = zaehler.get(s) if s else None
        e["dublette"] = bool(s and zaehler.get(s, 0) > 1)
    eintraege.sort(key=lambda e: (not e["ordner"], e["name"].casefold()))
    dateien = [e for e in eintraege if not e["ordner"]]
    return {"quelle": q["id"], "pfad": rel, "voll": voll, "eintraege": eintraege, "indexiert": (len(shas) == len(dateien)) if dateien else True}


def _liste_rclone(q, rel):
    ziel = "%s:%s" % (q["remote"], "/".join(p for p in [q.get("pfad", ""), rel.strip("/")] if p))
    out = _rclone("lsjson", ziel, "--max-depth", "1", "--hash", timeout=120)
    eintraege = []
    for e in json.loads(out or "[]"):
        try:
            mt = time.mktime(time.strptime(e.get("ModTime", "")[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            mt = 0
        h = e.get("Hashes") or {}
        eintraege.append({"name": e["Name"], "ordner": bool(e.get("IsDir")), "groesse": max(e.get("Size") or 0, 0), "mtime": mt,
                          "pfad": ziel.rstrip("/") + "/" + e["Name"], "sha256": h.get("sha256") or h.get("SHA-256"), "fundorte": None, "dublette": False})
    eintraege.sort(key=lambda e: (not e["ordner"], e["name"].casefold()))
    return {"quelle": q["id"], "pfad": rel, "voll": ziel, "eintraege": eintraege, "indexiert": False}


@router.get("/liste")
def liste(quelle: str, pfad: str = "", mandant: str = MANDANT):
    q = _quelle(mandant, quelle)
    return _liste_ordner(q, pfad, mandant) if q["art"] == "ordner" else _liste_rclone(q, pfad)


@router.get("/vorschau")
def vorschau(quelle: str, pfad: str, mandant: str = MANDANT):
    q = _quelle(mandant, quelle)
    if q["art"] != "ordner":
        return {"art": "keine", "hinweis": "Vorschau fuer Online-Drives folgt (rclone cat)."}
    voll = _sicher(q["pfad"], pfad)
    if not os.path.isfile(voll):
        raise HTTPException(404, "Datei nicht gefunden")
    groesse = os.path.getsize(voll)
    mime = mimetypes.guess_type(voll)[0] or "application/octet-stream"
    name = os.path.basename(voll).lower()
    if mime.startswith("image/") and groesse <= 4 * BLOCK:
        with open(voll, "rb") as f:
            return {"art": "bild", "mime": mime, "data": "data:%s;base64,%s" % (mime, base64.b64encode(f.read()).decode()), "groesse": groesse}
    if name.endswith(".pdf") and groesse <= 8 * BLOCK:
        with open(voll, "rb") as f:
            return {"art": "pdf", "data": "data:application/pdf;base64," + base64.b64encode(f.read()).decode(), "groesse": groesse}
    if mime.startswith("text/") or name.endswith((".md", ".json", ".jsonl", ".csv", ".txt", ".log", ".yaml", ".yml", ".py", ".js", ".xml", ".eml")):
        with open(voll, "rb") as f:
            roh = f.read(32 * 1024)
        return {"art": "text", "inhalt": roh.decode("utf-8", "replace"), "abgeschnitten": groesse > len(roh), "groesse": groesse}
    return {"art": "keine", "mime": mime, "groesse": groesse}


@router.post("/register")
def register(body: dict):
    """Quelle (Ordner) in das Mandanten-Register aufnehmen: sha256 je Datei — Grundlage fuer Dubletten."""
    mandant = body.get("mandant") or MANDANT
    q = _quelle(mandant, body.get("quelle", ""))
    if q["art"] != "ordner":
        raise HTTPException(400, "Register nur fuer lokale Ordner")
    voll = _sicher(q["pfad"], body.get("pfad", ""))
    con = _register_con(mandant)
    n = 0
    with bp.Protokoll(bp.pfad_fuer(mandant)) as prot:
        prot.schreiben("notiz", werkzeug="trove-plugin", mandant=mandant, meldung="Register-Lauf beginnt: " + voll)
        for wurzel, dirs, files in os.walk(voll):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                p = os.path.join(wurzel, fn)
                try:
                    st = os.stat(p)
                    row = con.execute("select sha256 from datei where pfad=? and groesse=? and abs(mtime-?)<0.01", (p, st.st_size, st.st_mtime)).fetchone()
                    if row:
                        continue
                    s = _sha(p)
                    con.execute("insert into datei values(?,?,?,?,?,?)", (q["id"], p, fn, st.st_size, st.st_mtime, s))
                    prot.schreiben("gesehen", quelle=p, sha256=s, bytes_=st.st_size, werkzeug="trove-plugin", mandant=mandant, quelle_id=q["id"])
                    n += 1
                    if n % 500 == 0:
                        con.commit()
                except OSError as ex:
                    prot.schreiben("fehler", quelle=p, werkzeug="trove-plugin", mandant=mandant, meldung=str(ex))
        con.commit()
        prot.schreiben("notiz", werkzeug="trove-plugin", mandant=mandant, meldung="Register-Lauf beendet: %d neue Dateien" % n)
        prot.anker_setzen(mandant, "register")
    gesamt = con.execute("select count(*) from datei").fetchone()[0]
    con.close()
    return {"ok": True, "neu": n, "gesamt": gesamt}


def _kopiere_datei(prot, mandant, lauf, von, nach, werkzeug="trove-plugin"):
    """Eine lokale Datei verifiziert kopieren. Nie ueberschreiben. Liefert Status-String."""
    tmp = nach + ".teil"
    if os.path.exists(nach):
        alt = _sha(nach); neu = _sha(von)
        if alt == neu:
            prot.schreiben("gesehen", quelle=von, ziel=nach, sha256=neu, bytes_=os.path.getsize(von), werkzeug=werkzeug, mandant=mandant, lauf=lauf, status_alt="schon_da")
            return "schon_da"
        prot.schreiben("fehler", quelle=von, ziel=nach, sha256=neu, bytes_=os.path.getsize(von), werkzeug=werkzeug, mandant=mandant, lauf=lauf,
                       meldung="konflikt: Ziel vorhanden mit anderem Inhalt, nicht ueberschrieben", status_alt="konflikt", sha_vorher=alt)
        return "konflikt"
    os.makedirs(os.path.dirname(nach), exist_ok=True)
    h = hashlib.sha256()
    try:
        with open(von, "rb") as src, open(tmp, "wb") as dst:
            for b in iter(lambda: src.read(BLOCK), b""):
                h.update(b); dst.write(b)
            dst.flush(); os.fsync(dst.fileno())
        os.replace(tmp, nach)
        try:
            st = os.stat(von); os.utime(nach, (st.st_atime, st.st_mtime))
        except OSError:
            pass
    except Exception as ex:
        if os.path.exists(tmp):
            os.remove(tmp)
        prot.schreiben("fehler", quelle=von, ziel=nach, werkzeug=werkzeug, mandant=mandant, lauf=lauf, meldung=str(ex) or type(ex).__name__)
        return "fehler"
    sha = h.hexdigest()
    groesse = os.path.getsize(nach)
    prot.schreiben("kopiert", quelle=von, ziel=nach, sha256=sha, bytes_=groesse, werkzeug=werkzeug, mandant=mandant, lauf=lauf)
    zurueck = _sha(nach)
    if zurueck != sha:
        prot.schreiben("fehler", quelle=nach, ziel=nach, sha256=zurueck, bytes_=groesse, werkzeug=werkzeug, mandant=mandant, lauf=lauf, meldung="Gegenprobe fehlgeschlagen")
        return "fehler"
    prot.schreiben("verifiziert", quelle=nach, ziel=nach, sha256=sha, bytes_=groesse, werkzeug=werkzeug, mandant=mandant, lauf=lauf, meldung="von der Platte zurueckgelesen, sha gleich")
    return "verifiziert"


def _dateien_unter(voll):
    if os.path.isfile(voll):
        yield voll, os.path.basename(voll)
        return
    for wurzel, dirs, files in os.walk(voll):
        for fn in files:
            p = os.path.join(wurzel, fn)
            yield p, os.path.relpath(p, os.path.dirname(voll))


@router.post("/aktion")
def aktion(body: dict):
    """art: kopieren | verschieben | sync. von/nach: {quelle, pfad}. namen: Eintraege im von-Ordner (leer bei sync = ganzer Ordner)."""
    mandant = body.get("mandant") or MANDANT
    art = body.get("art")
    if art not in ("kopieren", "verschieben", "sync"):
        raise HTTPException(400, "art muss kopieren, verschieben oder sync sein")
    qv, qn = _quelle(mandant, body["von"]["quelle"]), _quelle(mandant, body["nach"]["quelle"])
    if qn["art"] != "ordner" or qn.get("nur_lesen"):
        raise HTTPException(400, "Ziel muss ein beschreibbarer lokaler Ordner sein (Welt A). Online-Drives sind nur lesend.")
    if art == "verschieben" and (qv["art"] != "ordner" or qv.get("nur_lesen")):
        raise HTTPException(400, "Verschieben nur aus beschreibbaren lokalen Ordnern (Welt A). Aus Online-Drives/Welt B wird nur kopiert.")
    nach_ordner = _sicher(qn["pfad"], body["nach"].get("pfad", ""))
    lauf = "%s-%s" % (art, time.strftime("%Y%m%d-%H%M%S"))
    ergebnis = []
    with bp.Protokoll(bp.pfad_fuer(mandant)) as prot:
        prot.schreiben("notiz", werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="Aktion %s beginnt: %s -> %s" % (art, qv["name"], nach_ordner))
        if qv["art"] == "rclone":
            basis = "%s:%s" % (qv["remote"], "/".join(p for p in [qv.get("pfad", ""), body["von"].get("pfad", "").strip("/")] if p))
            namen = body.get("namen") or [""]
            for n in namen:
                src = basis.rstrip("/") + ("/" + n if n else "")
                ziel_pfad = os.path.join(nach_ordner, n) if n else nach_ordner
                try:
                    _rclone("copyto", src, ziel_pfad, "--ignore-existing", timeout=3600)
                    for p, rel in _dateien_unter(ziel_pfad):
                        s = _sha(p)
                        prot.schreiben("kopiert", quelle=src.rstrip("/") + ("/" + rel.replace("\\", "/") if os.path.isdir(ziel_pfad) else ""), ziel=p, sha256=s, bytes_=os.path.getsize(p),
                                       werkzeug="trove-plugin/rclone", mandant=mandant, lauf=lauf, meldung="Quell-Pruefsumme nicht geprueft (rclone copyto); Ziel-sha gerechnet")
                    ergebnis.append({"name": n or "(Ordner)", "status": "kopiert"})
                except HTTPException as ex:
                    prot.schreiben("fehler", quelle=src, ziel=ziel_pfad, werkzeug="trove-plugin/rclone", mandant=mandant, lauf=lauf, meldung=str(ex.detail))
                    ergebnis.append({"name": n, "status": "fehler", "meldung": str(ex.detail)})
        else:
            von_ordner = _sicher(qv["pfad"], body["von"].get("pfad", ""))
            if os.path.commonpath([von_ordner, nach_ordner]) == von_ordner and art in ("verschieben", "sync") and not body.get("namen"):
                raise HTTPException(400, "Ziel liegt innerhalb der Quelle")
            namen = body.get("namen") or ([os.path.basename(von_ordner)] if art != "sync" else [None])
            for n in namen:
                start = os.path.join(von_ordner, n) if n else von_ordner
                if not os.path.exists(start):
                    ergebnis.append({"name": n, "status": "fehlt"}); continue
                zusammen = {"verifiziert": 0, "schon_da": 0, "konflikt": 0, "fehler": 0}
                for p, rel in _dateien_unter(start):
                    ziel_pfad = os.path.join(nach_ordner, rel) if n else os.path.join(nach_ordner, os.path.relpath(p, von_ordner))
                    st = _kopiere_datei(prot, mandant, lauf, p, ziel_pfad)
                    zusammen[st] = zusammen.get(st, 0) + 1
                    if art == "verschieben" and st in ("verifiziert", "schon_da"):
                        os.remove(p)
                        prot.schreiben("verschoben", quelle=p, ziel=ziel_pfad, sha256=_sha(ziel_pfad), bytes_=os.path.getsize(ziel_pfad), werkzeug="trove-plugin",
                                       mandant=mandant, lauf=lauf, meldung="Quelle nach bestandener Gegenprobe entfernt")
                if art == "verschieben" and os.path.isdir(start) and zusammen["konflikt"] == 0 and zusammen["fehler"] == 0:
                    for wurzel, dirs, files in os.walk(start, topdown=False):
                        if not os.listdir(wurzel):
                            os.rmdir(wurzel)
                ergebnis.append({"name": n or "(Ordner)", **zusammen})
        prot.schreiben("notiz", werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="Aktion %s beendet: %s" % (art, json.dumps(ergebnis, ensure_ascii=False)[:800]))
        prot.anker_setzen(mandant, lauf)
    return {"ok": True, "lauf": lauf, "ergebnis": ergebnis}


@router.get("/spur")
def spur(datei: str, mandant: str = MANDANT):
    r = bp.spur(bp.pfad_fuer(mandant), datei)
    kurz = lambda liste: [{k: e.get(k) for k in ("nr", "zeit", "ereignis", "quelle", "ziel", "meldung", "lauf", "werkzeug")} for e in liste[:200]]
    return {"datei": datei, "sha256": r["sha256"], "direkt": kurz(r["direkt"]), "rueckwaerts": kurz(r["rueckwaerts"]), "vorwaerts": kurz(r["vorwaerts"]),
            "gleicher_inhalt": kurz(r["gleicher_inhalt"]), "abgeschnitten": r["abgeschnitten"]}


@router.post("/aenderungen")
def aenderungen_lauf(body: dict):
    """F2: Stand eines Ordners aufnehmen und mit dem letzten Lauf vergleichen (neu/weg/verschoben/aussen geaendert)."""
    mandant = body.get("mandant") or MANDANT
    q = _quelle(mandant, body.get("quelle", ""))
    if q["art"] != "ordner":
        raise HTTPException(400, "F2-Lauf nur fuer lokale Ordner")
    wurzel = _sicher(q["pfad"], body.get("pfad", ""))
    ort = "q-" + q["id"] + ("-" + hashlib.sha1(wurzel.casefold().encode()).hexdigest()[:6] if body.get("pfad") else "")
    r = aenderungen.lauf(mandant, ort, wurzel, werkzeug="trove-plugin/f2")
    b = r["befunde"]
    return {"ok": True, "lauf": r["lauf"], "erster_lauf": r["erster_lauf"], "dateien": r["dateien"], "gerechnet": r["gerechnet"],
            "zusammen": {k: (len(v) if isinstance(v, list) else v) for k, v in b.items()},
            "befunde": {k: v[:100] for k, v in b.items() if isinstance(v, list)}}


# ---------- F10: Auftraege an die KI ueber das Kanban (Freigabe durch Menschen) ----------

HERMES = os.environ.get("TROVE_HERMES", os.path.join(os.environ.get("LOCALAPPDATA", ""), r"hermes\bin\hermes.exe"))


def _hermes(*args, timeout=120):
    exe = HERMES if os.path.exists(HERMES) else "hermes"
    r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise HTTPException(502, "hermes: " + (r.stderr.strip()[-400:] or r.stdout.strip()[-400:] or "Fehler %d" % r.returncode))
    return r.stdout


def _hermes_json(*args, timeout=120):
    out = _hermes(*args, "--json", timeout=timeout)
    i = min([k for k in (out.find("{"), out.find("[")) if k >= 0] or [0])
    return json.loads(out[i:])


def _auftraege_pfad(mandant):
    return os.path.join(_mandant_dir(mandant), "auftraege.json")


def _auftraege_lesen(mandant):
    p = _auftraege_pfad(mandant)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return []


def _auftraege_speichern(mandant, liste):
    p = _auftraege_pfad(mandant)
    with open(p + ".neu", "w", encoding="utf-8") as f:
        json.dump(liste, f, ensure_ascii=False, indent=1)
    os.replace(p + ".neu", p)


@router.post("/auftrag")
def auftrag_anlegen(body: dict):
    """Karte im Hermes-Kanban anlegen — gesperrt (blocked), bis ein Mensch sie freigibt."""
    mandant = body.get("mandant") or MANDANT
    titel = (body.get("titel") or "").strip()
    if not titel:
        raise HTTPException(400, "Titel fehlt")
    text = (body.get("text") or "").strip()
    assignee = (body.get("assignee") or "schnapper").strip()
    kontext = body.get("kontext") or {}
    rumpf = text + "\n\n--\nAngelegt aus dem Trove-Tab (Datei-Zentrale), Mandant %s. Kontext: %s\nRegeln: nichts loeschen, nichts ueberschreiben; Ergebnis als Kommentar auf dieser Karte, dann Karte auf done." % (mandant, json.dumps(kontext, ensure_ascii=False))
    tmp = os.path.join(_mandant_dir(mandant), "auftrag_%d.txt" % int(time.time()))
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(rumpf)
    try:
        karte = _hermes_json("kanban", "create", "Trove: " + titel, "--body-file", tmp, "--assignee", assignee, "--initial-status", "blocked",
                             "--workspace", "dir:" + TROVE_REPO, "--max-retries", "1", "--max-runtime", "900", "--skill", "ponytail")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    kid = karte.get("id")
    eintrag = {"id": kid, "titel": titel, "assignee": assignee, "zeit": _jetzt(), "status": "blocked", "protokolliert": [], "kontext": kontext}
    _auftraege_speichern(mandant, _auftraege_lesen(mandant) + [eintrag])
    bp.schreiben(bp.pfad_fuer(mandant), ereignis="notiz", werkzeug="trove-plugin/kanban", mandant=mandant, lauf=kid,
                 meldung="Auftrag an die KI angelegt (gesperrt, wartet auf Freigabe durch Menschen): " + titel, karte=kid, assignee=assignee, kontext=kontext)
    return eintrag


@router.post("/auftrag/{kid}/freigeben")
def auftrag_freigeben(kid: str, body: dict = None):
    mandant = (body or {}).get("mandant") or MANDANT
    _hermes("kanban", "unblock", kid, "--reason", "Freigabe durch Mensch im Trove-Tab")
    try:
        _hermes("kanban", "dispatch", "--max", "1", timeout=60)
    except HTTPException:
        pass
    bp.schreiben(bp.pfad_fuer(mandant), ereignis="notiz", werkzeug="trove-plugin/kanban", mandant=mandant, lauf=kid, meldung="Auftrag freigegeben durch Mensch", karte=kid)
    return {"ok": True}


@router.post("/auftrag/{kid}/archivieren")
def auftrag_archivieren(kid: str, body: dict = None):
    mandant = (body or {}).get("mandant") or MANDANT
    _hermes("kanban", "archive", kid)
    liste = _auftraege_lesen(mandant)
    for a in liste:
        if a["id"] == kid:
            a["status"] = "archived"
    _auftraege_speichern(mandant, liste)
    bp.schreiben(bp.pfad_fuer(mandant), ereignis="notiz", werkzeug="trove-plugin/kanban", mandant=mandant, lauf=kid, meldung="Auftrag archiviert", karte=kid)
    return {"ok": True}


@router.get("/auftraege")
def auftraege(mandant: str = MANDANT):
    """Trove-Karten mit Stand; Ergebnisse (Kommentare, Zusammenfassung) fertiger Karten landen einmalig im Belegprotokoll."""
    liste = _auftraege_lesen(mandant)
    try:
        board = {k["id"]: k for k in _hermes_json("kanban", "ls", timeout=60)}
    except HTTPException as ex:
        return {"auftraege": liste, "fehler": str(ex.detail)}
    geaendert = False
    for a in liste:
        k = board.get(a["id"])
        if k is None:
            if a.get("status") != "archived":
                a["status"] = "archived"; geaendert = True
            continue
        if k.get("status") != a.get("status"):
            a["status"] = k.get("status"); geaendert = True
        a["zusammenfassung"] = (k.get("result") or "")[:600]
        fehler = k.get("last_failure_error") or ""
        if a["status"] == "blocked" and fehler and fehler[:200] != a.get("letzter_fehler"):
            a["letzter_fehler"] = fehler[:200]; geaendert = True
            bp.schreiben(bp.pfad_fuer(mandant), ereignis="fehler", werkzeug="trove-plugin/kanban", mandant=mandant, lauf=a["id"],
                         meldung="Auftrag fehlgeschlagen, Karte wieder gesperrt: " + fehler[:800], karte=a["id"])
        if a["status"] in ("done", "review") and a["status"] not in a.get("protokolliert", []):
            try:
                detail = _hermes_json("kanban", "show", a["id"], timeout=60)
            except HTTPException:
                detail = {}
            kommentare = detail.get("comments") or []
            letzter = (kommentare[-1].get("body") or kommentare[-1].get("text") or "") if kommentare else ""
            bp.schreiben(bp.pfad_fuer(mandant), ereignis="notiz", werkzeug="trove-plugin/kanban", mandant=mandant, lauf=a["id"],
                         meldung="Ergebnis des Auftrags (%s): %s" % (a["status"], (a["zusammenfassung"] or letzter or "(kein Text)")[:1500]), karte=a["id"],
                         kommentare=len(kommentare), ergebnis_kommentar=letzter[:1500])
            a.setdefault("protokolliert", []).append(a["status"]); a["ergebnis_kommentar"] = letzter[:1500]; geaendert = True
    if geaendert:
        _auftraege_speichern(mandant, liste)
    return {"auftraege": list(reversed(liste))}


# ---------- F3/F4/F6: Lesen, Verproben, Stapel zerlegen (alles lokal, keine bezahlten Token) ----------
PYTHON = os.environ.get("TROVE_PYTHON", os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\Python\Python313\python.exe"))


def _werkzeug(skript, *args, timeout=1800):
    """OCR/PDF laufen im System-Python (pymupdf, rapidocr) als Unterprozess — der Hermes-Server hat ein eigenes Python ohne diese Pakete."""
    if not os.path.exists(PYTHON):
        raise HTTPException(500, "System-Python nicht gefunden: " + PYTHON + " (TROVE_PYTHON setzen)")
    try:
        r = subprocess.run([PYTHON, os.path.join(TROVE_REPO, skript), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=TROVE_REPO,
                           env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})  # Windows-Codepage: Umlaute sauber
    except subprocess.TimeoutExpired:
        raise HTTPException(504, skript + ": Zeit abgelaufen")
    if r.returncode != 0:
        raise HTTPException(500, skript + " fehlgeschlagen: " + (r.stderr or r.stdout)[-800:])
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise HTTPException(500, skript + ": keine JSON-Ausgabe: " + r.stdout[-400:])


def _sp():
    import spiegel_erpnext  # nur urllib, laeuft im Server-Python
    return spiegel_erpnext


def _quell_datei(mandant, body):
    q = _quelle(mandant, body.get("quelle", ""))
    if q["art"] != "ordner":
        raise HTTPException(400, "Lesen nur aus lokalem Ordner (rclone-Quellen zuerst holen)")
    voll = _sicher(q["pfad"], body.get("pfad", ""))
    if not os.path.isfile(voll):
        raise HTTPException(404, "Datei nicht gefunden: " + body.get("pfad", ""))
    return voll


@router.post("/lesen")
def lesen(body: dict):
    """F3: Lese-Treppe (E-Rechnung > Textlayer > OCR) fuer eine Datei; Zwilling speichern, F8 'gelesen'; dann F4 Verprobung."""
    mandant = body.get("mandant") or MANDANT
    import verprobung as vp  # reine Regeln, kein ONNX
    voll = _quell_datei(mandant, body)
    args = [voll, "--mandant", mandant, "--ocr", body.get("ocr") or "rapidocr", "--max-seiten", str(int(body.get("max_seiten") or 10)), "--json"]
    if body.get("erzwinge_ocr"):
        args.append("--erzwinge-ocr")
    erg = _werkzeug("lese_treppe.py", *args)
    v = vp.verproben_zwilling(mandant, erg["zwilling"])
    return {"ok": True, "zwilling": erg["zwilling"], "sha256": erg["sha256"], "methode": erg["methode"], "seiten": erg["seiten"], "dauer_s": round(erg["dauer_s"], 2),
            "felder": erg["felder"], "verprobung": {k: v[k] for k in ("bestanden", "sicher", "gruende", "regeln")}, "text_anfang": erg.get("text_anfang", "")}


@router.post("/stapel")
def stapel_zerlegen(body: dict):
    """F6: mehrseitiges PDF in Belege zerlegen (Stirling-PDF), Ergebnis unter <mandant>\stapel\<sha12>\, F8 'zerlegt' je Beleg."""
    mandant = body.get("mandant") or MANDANT
    voll = _quell_datei(mandant, body)
    r = _werkzeug("stapel.py", voll, "--mandant", mandant, "--json")
    return {"ok": True, **{k: v for k, v in r.items() if k != "seiten_text"}}


@router.get("/historie")
def historie(sha256: str = "", datei: str = "", mandant: str = MANDANT):
    """F11: Geschaeftshistorie eines Inhalts aus dem ERPNext-Spiegel (plus Zwilling/Prueflist lokal)."""
    sp = _sp()
    _mandant_pruefen(mandant)
    if not sha256 and datei:
        shas = bp.spur(bp.pfad_fuer(mandant), datei)["sha256"]
        sha256 = shas[-1] if shas else ""
    if not sha256:
        raise HTTPException(400, "sha256 oder datei angeben")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise HTTPException(400, "sha256 ungueltig")
    try:
        r = sp.historie(sha256)
    except Exception as ex:  # ERPNext nicht erreichbar -> Datei bleibt Wahrheit
        r = {"ereignisse": None, "beleg": None, "fehler": str(ex)[:200]}
    zp = os.path.join(TROVE_DATEN, mandant, "zwillinge", sha256 + ".json")
    if os.path.exists(zp):
        with open(zp, encoding="utf-8") as f:
            z = json.load(f)
        r["zwilling"] = {k: z.get(k) for k in ("datei", "methode", "seiten", "felder", "zeit")}
    return {"sha256": sha256, **r}


@router.post("/spiegeln")
def spiegeln(body: dict = None):
    """F11: Belegprotokoll-Zeilen seit letztem Stand nach ERPNext spiegeln und Beleg-Saetze aktualisieren."""
    body = body or {}
    mandant = body.get("mandant") or MANDANT
    sp = _sp()
    try:
        a = sp.spiegeln(mandant, int(body.get("max") or 0) or None)
        b = sp.belege_aktualisieren(mandant, int(body.get("max_belege") or 0) or None) if body.get("belege", True) else {}
    except Exception as ex:
        raise HTTPException(502, "ERPNext: " + str(ex)[:300])
    try:
        bp.schreiben(bp.pfad_fuer(mandant), ereignis="notiz", werkzeug="trove-plugin/f11", mandant=mandant,
                     meldung="ERPNext-Spiegel: %d Ereignisse bis nr %d, %s Belege aktualisiert" % (a["gespiegelt"], a["letzte_nr"], b.get("belege_aktualisiert", "-")))
    except bp.ProtokollGesperrt:
        pass  # Spiegel ist gelaufen; Notiz ist Beiwerk
    return {"ok": True, **a, **b}


# ---------- F5: E-Mail-Archiv (Postfaecher abholen, Anhaenge ins Protokoll) ----------
@router.get("/mail/postfaecher")
def mail_postfaecher(mandant: str = MANDANT):
    import mail_abholer as ma
    _mandant_pruefen(mandant)
    out = []
    for pf in ma.postfaecher(mandant):
        st = ma._stand(mandant, pf["id"])
        wurzel = os.path.join(TROVE_DATEN, mandant, "mail", pf["id"])
        mails = sum(1 for r, d, f in os.walk(wurzel) if "mail.eml" in f) if os.path.isdir(wurzel) else 0
        out.append({"id": pf["id"], "art": pf["art"], "konto": st.get("konto"), "mails_archiviert": mails, "stand": {k: v for k, v in st.items() if k != "konto"}, "ablage": wurzel})
    return {"postfaecher": out}


@router.post("/mail/abholen")
def mail_abholen(body: dict = None):
    """F5: alle (oder ein) Postfach abholen; laeuft als Unterprozess im System-Python, damit der Server nicht blockiert ist."""
    body = body or {}
    mandant = _mandant_pruefen(body.get("mandant") or MANDANT)
    args = ["--mandant", mandant, "--json"]
    if body.get("postfach"):
        args += ["--postfach", body["postfach"]]
    if body.get("max"):
        args += ["--max", str(int(body["max"]))]
    if body.get("abfrage"):
        args.append("--abfrage=" + str(body["abfrage"]))
    if body.get("nur_anhaenge"):
        args.append("--nur-anhaenge")
    if body.get("alles"):
        args.append("--alles")
    r = _werkzeug("mail_abholer.py", *args, timeout=7200)
    return {"ok": True, "ergebnis": r}


# ---------- F7: Unterschreiben und versenden (DocuSeal) ----------
def _ds():
    import docuseal  # nur urllib
    return docuseal


@router.get("/docuseal/vorlagen")
def docuseal_vorlagen():
    try:
        return {"vorlagen": _ds().vorlagen()}
    except Exception as ex:
        raise HTTPException(502, "DocuSeal: " + str(ex)[:300])


@router.post("/docuseal/senden")
def docuseal_senden(body: dict):
    """Vorgang aus Vorlage anlegen; 'quelle'+'pfad' = Original (Rueckverfolgung), 'an' = Unterzeichner, 'mail' = DocuSeal mailt selbst."""
    mandant = body.get("mandant") or MANDANT
    beleg = _quell_datei(mandant, body) if body.get("quelle") and body.get("pfad") else None
    if not body.get("an") or not body.get("vorlage"):
        raise HTTPException(400, "vorlage und an (E-Mail) angeben")
    try:
        return {"ok": True, **_ds().senden(mandant, int(body["vorlage"]), body["an"], body.get("name", ""), body.get("rolle"), beleg, bool(body.get("mail")), body.get("betreff"))}
    except Exception as ex:
        raise HTTPException(502, "DocuSeal: " + str(ex)[:300])


@router.post("/docuseal/abholen")
def docuseal_abholen(body: dict = None):
    body = body or {}
    try:
        return {"ok": True, "vorgaenge": _ds().abholen(body.get("mandant") or MANDANT, body.get("vorgang"))}
    except Exception as ex:
        raise HTTPException(502, "DocuSeal: " + str(ex)[:300])


# ---------- F15: eine Suche ueber beide Welten ----------
@router.get("/suche")
def suche(q: str, mandant: str = MANDANT, n: int = 30):
    import suche as su
    _mandant_pruefen(mandant)
    return su.suchen(q, mandant, max(5, min(int(n), 100)))
