"""Trove — Backend des Hermes-Desktop-Tabs. Mount: /api/plugins/reraise-trove/

Sources (local folders), two panes (list + preview), actions copy / move / sync. Every action goes through the
receipt log (F8) with a read-back check (sha256 of the file as written to disk). Duplicates via the register (F1, SQLite per client).
Nothing is overwritten. Writing is per source folder: a source is read-only unless it was explicitly added as writable.
Copy needs a writable target; move additionally deletes the source file after the read-back check, so it needs a writable source too.
Trove never asks for or stores credentials; mail and cloud files reach it as files in a watched folder (put there by Hermes agents).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

from fastapi import APIRouter, HTTPException

HIER = os.path.dirname(os.path.abspath(__file__))
# All Trove modules ship inside this directory (plugin/dashboard), so installing only the plugin directory is enough.
TROVE_REPO = HIER  # kept as a name for code that needs "where Trove's own scripts live"
if HIER not in sys.path:
    sys.path.insert(0, HIER)
import konfig  # noqa: E402
import belegprotokoll as bp  # noqa: E402
import aenderungen  # noqa: E402
import gesundheit  # noqa: E402
import gemeinsam as gm  # noqa: E402
import mcp_queue  # noqa: E402


TROVE_DATEN = bp.WURZEL  # eine Datenwurzel (Umgebungsvariable TROVE_DATEN, siehe belegprotokoll)
RE_MANDANT = r"[a-z0-9][a-z0-9_-]{0,40}"  # muss gleich gemeinsam.RE_MANDANT sein (Kopie: Plugin laedt ohne F16-Module)
router = APIRouter()
BLOCK = 1 << 20
MANDANT = konfig.mandant()  # trove.json / TROVE_MANDANT
VERSION = "0.2.0rc1"


def _jetzt():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _sha(pfad):
    h = hashlib.sha256()
    with open(pfad, "rb") as f:
        for b in iter(lambda: f.read(BLOCK), b""):
            h.update(b)
    return h.hexdigest()


def _wiki_oder_none():
    """Path of the knowledge target (wiki folder) or None if it cannot be resolved."""
    import wissenskarte as wk
    try:
        return wk.wiki_wurzel(anlegen=False)
    except ValueError:
        return None


def _mandant_pruefen(mandant):
    if not re.fullmatch(RE_MANDANT, mandant or ""):
        raise HTTPException(400, "client: a-z 0-9 _ - (1-41 characters)")
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
    raise HTTPException(404, "Unknown source %s" % qid)






_LOG = logging.getLogger("trove")


def _ohne_pfade(text):
    """Fehlertext nach aussen: nur bekannte Wurzeln (Datenwurzel, Repo, Hermes, Temp, Home) werden zu <daten>/<repo>/<hermes>/
    <temp>/<home>; Daten wie 20/10/2026 und URLs bleiben. Laengste Wurzel zuerst; auch mit / und JSON-doppelten \\."""
    t = str(text)
    wurzeln = (("<daten>", TROVE_DATEN), ("<repo>", TROVE_REPO), ("<hermes>", konfig.hermes_home()), ("<temp>", tempfile.gettempdir()),
               ("<home>", os.path.expanduser("~")))
    varianten = {(v, name) for name, w in wurzeln if w and len(w) > 3
                 for v in (w, os.path.realpath(w), w.replace("\\", "/"), w.replace("\\", "\\\\"))}
    for v, name in sorted(varianten, key=lambda x: -len(x[0])):
        t = re.sub(re.escape(v), lambda m: name, t, flags=re.I if os.name == "nt" else 0)
    return t


def _fehler(code, text):
    """HTTP-Fehler ohne bekannte Pfade nach aussen; der volle Text bleibt im Server-Log."""
    _LOG.warning("trove HTTP %s: %s", code, text)
    return HTTPException(code, _ohne_pfade(text))










def _unter_real(pfad, wurzel):
    """Liegt pfad nach Aufloesen aller Verknuepfungen (realpath) unter der aufgeloesten wurzel?"""
    r, w = os.path.normcase(os.path.realpath(pfad)), os.path.normcase(os.path.realpath(wurzel))
    try:
        return os.path.commonpath([w, r]) == w
    except ValueError:  # anderes Laufwerk
        return False


def _sicher(wurzel, rel):
    """Relativen Pfad unter der Quellwurzel aufloesen; nie darueber hinaus (auch nicht ueber Verknuepfungen)."""
    rel = (rel or "").replace("\\", "/").strip("/")
    voll = os.path.normpath(os.path.join(wurzel, *[s for s in rel.split("/") if s and s not in (".", "..")]))
    if os.path.commonpath([os.path.normpath(wurzel), voll]) != os.path.normpath(wurzel) or not _unter_real(voll, wurzel):
        raise HTTPException(400, "Path is outside the source folder")
    return voll


def _namen_pruefen(namen):
    """namen aus /aktion: nur Eintragsnamen im gewaehlten Ordner (Basename), keine Pfade, kein .. und nichts Absolutes."""
    if not isinstance(namen, list):
        raise HTTPException(400, "namen: list of entry names")
    for n in namen:
        if n in (None, ""):
            continue
        if (not isinstance(n, str) or n in (".", "..") or "/" in n or "\\" in n or "\0" in n or os.path.isabs(n)
                or os.path.splitdrive(n)[0] or os.path.basename(n) != n):
            raise HTTPException(400, "namen: entry names inside the folder only, no paths: %r" % (n,))
    return namen


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
    out = {}
    for s in set(shas):
        n = con.execute("select count(distinct pfad) from datei where sha256=?", (s,)).fetchone()[0]
        out[s] = n
    con.close()
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
    return {"ok": True, "mandant": MANDANT, "datenwurzel": TROVE_DATEN, "wiki": _wiki_oder_none(), "profile": gesundheit.hermes_profile(),
            "hermes_cli": _hermes_exe() is not None, "zeit": _jetzt(), "geraet": os.environ.get("COMPUTERNAME", ""),
            "pid": os.getpid(), "protokolle": prot, "version": VERSION}


@router.get("/gesundheit")
def gesundheit_route():
    return gesundheit.bericht()


@router.get("/quellen")
def quellen(mandant: str = MANDANT):
    return {"quellen": _quellen(mandant)}


@router.post("/quellen")
def quelle_anlegen(body: dict):
    """Add a local folder as a source. Read-only unless the caller explicitly sends nur_lesen=false (writable source)."""
    mandant = body.get("mandant") or MANDANT
    name = (body.get("name") or "").strip()
    if body.get("art") not in (None, "", "auto", "ordner"):
        raise HTTPException(400, "Only local folders can be added as sources.")
    pfad = os.path.abspath(os.path.expanduser(body.get("pfad") or ""))
    if not os.path.isdir(pfad):
        raise HTTPException(400, "Folder not found: " + pfad)
    q = {"id": hashlib.sha1(("ordner:" + pfad.casefold()).encode()).hexdigest()[:10], "art": "ordner", "name": name or pfad, "pfad": pfad,
         "nur_lesen": body.get("nur_lesen", True) is not False}  # fail-safe: only an explicit false makes the source writable
    liste = [x for x in _quellen(mandant) if x["id"] != q["id"]] + [q]
    _quellen_speichern(mandant, liste)
    bp.schreiben(bp.pfad_fuer(mandant), ereignis="notiz", werkzeug="trove-plugin", mandant=mandant,
                 meldung="Source added: %s (%s)" % (q["name"], "read-only" if q["nur_lesen"] else "writable"), quelle_id=q["id"], quelle_art=q["art"],
                 quelle_pfad=q.get("pfad", ""))
    return q


@router.post("/quellen/{qid}/schreibbar")
def quelle_schreibbar(qid: str, body: dict):
    """Switch one source between read-only and writable (the per-folder write permission)."""
    mandant = body.get("mandant") or MANDANT
    q = _quelle(mandant, qid)
    nur_lesen = body.get("nur_lesen") is not False
    liste = [({**x, "nur_lesen": nur_lesen} if x["id"] == qid else x) for x in _quellen(mandant)]
    _quellen_speichern(mandant, liste)
    bp.schreiben(bp.pfad_fuer(mandant), ereignis="notiz", werkzeug="trove-plugin", mandant=mandant,
                 meldung="Source %s is now %s" % (q["name"], "read-only" if nur_lesen else "writable"), quelle_id=qid)
    return {**q, "nur_lesen": nur_lesen}


@router.delete("/quellen/{qid}")
def quelle_entfernen(qid: str, mandant: str = MANDANT):
    q = _quelle(mandant, qid)
    _quellen_speichern(mandant, [x for x in _quellen(mandant) if x["id"] != qid])
    bp.schreiben(bp.pfad_fuer(mandant), ereignis="notiz", werkzeug="trove-plugin", mandant=mandant, meldung="Source removed (from the list only, no files touched): " + q["name"], quelle_id=qid)
    return {"ok": True}


def _liste_ordner(q, rel, mandant):
    wurzel = q["pfad"]
    voll = _sicher(wurzel, rel)
    if not os.path.isdir(voll):
        raise HTTPException(404, "Folder not found")
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




@router.get("/liste")
def liste(quelle: str, pfad: str = "", mandant: str = MANDANT):
    q = _quelle(mandant, quelle)
    return _liste_ordner(q, pfad, mandant)


@router.get("/vorschau")
def vorschau(quelle: str, pfad: str, mandant: str = MANDANT, seite: int = 0):
    q = _quelle(mandant, quelle)
    voll = _sicher(q["pfad"], pfad)
    if not os.path.isfile(voll):
        raise HTTPException(404, "File not found")
    groesse = os.path.getsize(voll)
    mime = mimetypes.guess_type(voll)[0] or "application/octet-stream"
    name = os.path.basename(voll).lower()
    if mime.startswith("image/") and groesse <= 4 * BLOCK:
        with open(voll, "rb") as f:
            return {"art": "bild", "mime": mime, "data": "data:%s;base64,%s" % (mime, base64.b64encode(f.read()).decode()), "groesse": groesse}
    if name.endswith(".pdf"):  # seitenweise als Bild (nur lesen); ohne pypdfium2 weiter unten der ganze Browser-Viewer
        try:
            return {"art": "pdf_seite", **_werkzeug("pdf_seite.py", voll, str(seite), timeout=60), "groesse": groesse}
        except HTTPException:
            pass
    if name.endswith(".pdf") and groesse <= 8 * BLOCK:
        with open(voll, "rb") as f:
            return {"art": "pdf", "data": "data:application/pdf;base64," + base64.b64encode(f.read()).decode(), "groesse": groesse}
    if mime.startswith("text/") or name.endswith((".md", ".json", ".jsonl", ".csv", ".txt", ".log", ".yaml", ".yml", ".py", ".js", ".xml", ".eml")):
        with open(voll, "rb") as f:
            roh = f.read(32 * 1024)
        text = roh.decode("utf-8", "replace")
        if name.endswith(".md") and text.startswith("---"):  # Zwilling / Wissenskarte: Kopf (Schluessel: Wert) und Text getrennt
            kopf, _, rest = text[3:].partition("\n---")
            return {"art": "karte", "kopf": [z.split(":", 1) for z in kopf.strip().splitlines() if ":" in z and not z.startswith(" ")],
                    "inhalt": rest.lstrip("\r\n"), "abgeschnitten": groesse > len(roh), "groesse": groesse}
        return {"art": "text", "inhalt": text, "abgeschnitten": groesse > len(roh), "groesse": groesse}
    return {"art": "keine", "mime": mime, "groesse": groesse}


@router.post("/register")
def register(body: dict):
    """Quelle (Ordner) in das Mandanten-Register aufnehmen: sha256 je Datei — Grundlage fuer Dubletten."""
    mandant = body.get("mandant") or MANDANT
    q = _quelle(mandant, body.get("quelle", ""))
    if q["art"] != "ordner":
        raise HTTPException(400, "Register works on local folders only")
    voll = _sicher(q["pfad"], body.get("pfad", ""))
    con = _register_con(mandant)
    n = wolke = 0
    with bp.Protokoll(bp.pfad_fuer(mandant)) as prot:
        prot.schreiben("notiz", werkzeug="trove-plugin", mandant=mandant, meldung="Register run starts: " + voll)
        for wurzel, dirs, files in os.walk(voll):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                p = os.path.join(wurzel, fn)
                if gm.nur_in_wolke(p, fn):  # nur in der Wolke (.icloud oder dataless): nicht hashen, kein Download
                    wolke += 1
                    continue
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
        prot.schreiben("notiz", werkzeug="trove-plugin", mandant=mandant, meldung="Register run finished: %d new files, %d only in the cloud (iCloud placeholders, not read)" % (n, wolke))
        prot.anker_setzen(mandant, "register")
    gesamt = con.execute("select count(*) from datei").fetchone()[0]
    con.close()
    return {"ok": True, "neu": n, "gesamt": gesamt, "nur_in_wolke": wolke}


def _kopiere_datei(prot, mandant, lauf, von, nach, werkzeug="trove-plugin"):
    """Eine lokale Datei verifiziert kopieren. Nie ueberschreiben. Liefert Status-String."""
    tmp = nach + ".teil"
    if os.path.exists(nach):
        alt = _sha(nach); neu = _sha(von)
        if alt == neu:
            prot.schreiben("gesehen", quelle=von, ziel=nach, sha256=neu, bytes_=os.path.getsize(von), werkzeug=werkzeug, mandant=mandant, lauf=lauf, status_alt="schon_da")
            return "schon_da"
        prot.schreiben("fehler", quelle=von, ziel=nach, sha256=neu, bytes_=os.path.getsize(von), werkzeug=werkzeug, mandant=mandant, lauf=lauf,
                       meldung="conflict: target exists with different content, not overwritten", status_alt="konflikt", sha_vorher=alt)
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
        prot.schreiben("fehler", quelle=nach, ziel=nach, sha256=zurueck, bytes_=groesse, werkzeug=werkzeug, mandant=mandant, lauf=lauf, meldung="read-back check failed")
        return "fehler"
    prot.schreiben("verifiziert", quelle=nach, ziel=nach, sha256=sha, bytes_=groesse, werkzeug=werkzeug, mandant=mandant, lauf=lauf, meldung="read back from disk, sha256 equal")
    return "verifiziert"


def _dateien_unter(voll):
    if os.path.isfile(voll):
        yield voll, os.path.basename(voll)
        return
    for wurzel, dirs, files in os.walk(voll):  # Verknuepfungen werden nie verfolgt (Ordner nicht, Dateien uebersprungen)
        for fn in files:
            p = os.path.join(wurzel, fn)
            if os.path.islink(p):
                continue
            yield p, os.path.relpath(p, os.path.dirname(voll))


@router.post("/aktion")
def aktion(body: dict):
    """art: kopieren | verschieben | sync. von/nach: {quelle, pfad}. namen: Eintraege im von-Ordner (leer bei sync = ganzer Ordner)."""
    mandant = body.get("mandant") or MANDANT
    art = body.get("art")
    if art not in ("kopieren", "verschieben", "sync"):
        raise HTTPException(400, "art must be kopieren (copy), verschieben (move) or sync")
    _namen_pruefen(body.get("namen") or [])
    qv, qn = _quelle(mandant, body["von"]["quelle"]), _quelle(mandant, body["nach"]["quelle"])
    if qn.get("nur_lesen", True):
        raise HTTPException(403, "The target source is read-only. Mark that folder as writable to copy into it.")
    if art == "verschieben" and qv.get("nur_lesen", True):
        raise HTTPException(403, "Move deletes files from the source, so the source folder must be marked writable. Copy works from a read-only source.")
    nach_ordner = _sicher(qn["pfad"], body["nach"].get("pfad", ""))
    lauf = "%s-%s" % (art, time.strftime("%Y%m%d-%H%M%S"))
    ergebnis = []
    with bp.Protokoll(bp.pfad_fuer(mandant)) as prot:
        prot.schreiben("notiz", werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="Action %s starts: %s -> %s" % (art, qv["name"], nach_ordner))
        von_ordner = _sicher(qv["pfad"], body["von"].get("pfad", ""))
        if os.path.commonpath([von_ordner, nach_ordner]) == von_ordner and art in ("verschieben", "sync") and not body.get("namen"):
            raise HTTPException(400, "Target is inside the source folder")
        namen = body.get("namen") or ([os.path.basename(von_ordner)] if art != "sync" else [None])
        for n in namen:
            if n and os.path.lexists(os.path.join(von_ordner, n)) and not _unter_real(os.path.join(von_ordner, n), von_ordner):
                raise HTTPException(400, "Path leaves the source folder (symlink): %s" % n)
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
                                   mandant=mandant, lauf=lauf, meldung="source removed after the read-back check passed")
            if art == "verschieben" and os.path.isdir(start) and zusammen["konflikt"] == 0 and zusammen["fehler"] == 0:
                for wurzel, dirs, files in os.walk(start, topdown=False):
                    if not os.listdir(wurzel):
                        os.rmdir(wurzel)
            ergebnis.append({"name": n or "(folder)", **zusammen})
        prot.schreiben("notiz", werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="Action %s finished: %s" % (art, json.dumps(ergebnis, ensure_ascii=False)[:800]))
        prot.anker_setzen(mandant, lauf)
    return {"ok": True, "lauf": lauf, "ergebnis": ergebnis}


@router.get("/spur")
def spur(datei: str, mandant: str = MANDANT):
    _mandant_pruefen(mandant)
    r = bp.spur(bp.pfad_fuer(mandant), datei)
    kurz = lambda liste: [{k: e.get(k) for k in ("nr", "zeit", "ereignis", "quelle", "ziel", "meldung", "lauf", "werkzeug")} for e in liste[:200]]
    return {"datei": datei, "sha256": r["sha256"], "direkt": kurz(r["direkt"]), "rueckwaerts": kurz(r["rueckwaerts"]), "vorwaerts": kurz(r["vorwaerts"]),
            "gleicher_inhalt": kurz(r["gleicher_inhalt"]), "abgeschnitten": r["abgeschnitten"]}


@router.post("/aenderungen")
def aenderungen_lauf(body: dict):
    """F2: Stand eines Ordners aufnehmen und mit dem letzten Lauf vergleichen (neu/weg/verschoben/aussen geaendert)."""
    mandant = body.get("mandant") or MANDANT
    q = _quelle(mandant, body.get("quelle", ""))
    wurzel = _sicher(q["pfad"], body.get("pfad", ""))
    ort = "q-" + q["id"] + ("-" + hashlib.sha1(wurzel.casefold().encode()).hexdigest()[:6] if body.get("pfad") else "")
    r = aenderungen.lauf(mandant, ort, wurzel, werkzeug="trove-plugin/f2")
    b = r["befunde"]
    return {"ok": True, "lauf": r["lauf"], "erster_lauf": r["erster_lauf"], "dateien": r["dateien"], "gerechnet": r["gerechnet"],
            "nur_in_wolke": r.get("nur_in_wolke", 0),
            "zusammen": {k: (len(v) if isinstance(v, list) else v) for k, v in b.items()},
            "befunde": {k: v[:100] for k, v in b.items() if isinstance(v, list)}}


# ---------- F10: Auftraege an die KI ueber das Kanban (Freigabe durch Menschen) ----------

def _hermes_exe():
    return konfig.hermes_exe()


def _hermes(*args, timeout=120):
    exe = _hermes_exe()
    if exe is None:
        raise HTTPException(502, "hermes CLI not found (not in PATH). Set its path with TROVE_HERMES.")
    r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise HTTPException(502, "hermes: " + (r.stderr.strip()[-400:] or r.stdout.strip()[-400:] or "error %d" % r.returncode))
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
    """Create a card on the Hermes kanban board -- blocked until a human releases it."""
    mandant = body.get("mandant") or MANDANT
    titel = (body.get("titel") or "").strip()
    if not titel:
        raise HTTPException(400, "Title is missing")
    text = (body.get("text") or "").strip()
    assignee = (body.get("assignee") or os.environ.get("TROVE_ASSIGNEE") or "").strip()  # Hermes profile; empty = Hermes default
    kontext = body.get("kontext") or {}
    rumpf = text + "\n\n--\nCreated from the Trove tab, client %s. Context: %s\nRules: delete nothing, overwrite nothing; post the result as a comment on this card, then set the card to done." % (mandant, json.dumps(kontext, ensure_ascii=False))
    tmp = os.path.join(_mandant_dir(mandant), "auftrag_%d.txt" % int(time.time()))
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(rumpf)
    try:
        args = ["kanban", "create", "Trove: " + titel, "--body-file", tmp, "--initial-status", "blocked",
                "--workspace", ("dir:" + os.environ["TROVE_ARBEITSORDNER"]) if os.environ.get("TROVE_ARBEITSORDNER") else "scratch",
                "--max-retries", "1", "--max-runtime", "900"]
        if assignee:
            args += ["--assignee", assignee]
        if os.environ.get("TROVE_SKILL"):  # optional Hermes skill for the agent, none by default
            args += ["--skill", os.environ["TROVE_SKILL"]]
        karte = _hermes_json(*args)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    kid = karte.get("id")
    eintrag = {"id": kid, "titel": titel, "assignee": assignee, "zeit": _jetzt(), "status": "blocked", "protokolliert": [], "kontext": kontext}
    _auftraege_speichern(mandant, _auftraege_lesen(mandant) + [eintrag])
    bp.schreiben(bp.pfad_fuer(mandant), ereignis="notiz", werkzeug="trove-plugin/kanban", mandant=mandant, lauf=kid,
                 meldung="Task for an agent created (blocked, waits for human release): " + titel, karte=kid, assignee=assignee, kontext=kontext)
    return eintrag


@router.post("/auftrag/{kid}/freigeben")
def auftrag_freigeben(kid: str, body: dict = None):
    mandant = (body or {}).get("mandant") or MANDANT
    if mcp_queue.ist_mcp(kid):  # request from an MCP client: a human click here is its only way to run
        try:
            return mcp_queue.freigeben(_mandant_pruefen(mandant), kid, sys.modules[__name__])
        except KeyError:
            raise HTTPException(404, "Unknown task %s" % kid)
        except ValueError as ex:
            raise HTTPException(409, str(ex))
    _hermes("kanban", "unblock", kid, "--reason", "Freigabe durch Mensch im Trove-Tab")
    try:
        _hermes("kanban", "dispatch", "--max", "1", timeout=60)
    except HTTPException:
        pass
    bp.schreiben(bp.pfad_fuer(mandant), ereignis="notiz", werkzeug="trove-plugin/kanban", mandant=mandant, lauf=kid, meldung="Task released by a human", karte=kid)
    return {"ok": True}


@router.post("/auftrag/{kid}/archivieren")
def auftrag_archivieren(kid: str, body: dict = None):
    mandant = (body or {}).get("mandant") or MANDANT
    if mcp_queue.ist_mcp(kid):
        try:
            return mcp_queue.ablehnen(_mandant_pruefen(mandant), kid)
        except ValueError as ex:
            raise HTTPException(409, str(ex))
    _hermes("kanban", "archive", kid)
    liste = _auftraege_lesen(mandant)
    for a in liste:
        if a["id"] == kid:
            a["status"] = "archived"
    _auftraege_speichern(mandant, liste)
    bp.schreiben(bp.pfad_fuer(mandant), ereignis="notiz", werkzeug="trove-plugin/kanban", mandant=mandant, lauf=kid, meldung="Task archived", karte=kid)
    return {"ok": True}


@router.get("/auftraege")
def auftraege(mandant: str = MANDANT):
    """Trove-Karten mit Stand; Ergebnisse (Kommentare, Zusammenfassung) fertiger Karten landen einmalig im Belegprotokoll."""
    liste = _auftraege_lesen(mandant)
    mcp = mcp_queue.fuer_tab(_mandant_pruefen(mandant))  # requests from MCP clients (mcp_server.py), blocked until released here
    try:
        board = {k["id"]: k for k in _hermes_json("kanban", "ls", timeout=60)}
    except HTTPException as ex:
        return {"auftraege": mcp + list(reversed(liste)), "fehler": str(ex.detail)}
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
                         meldung="Task failed, card blocked again: " + fehler[:800], karte=a["id"])
        if a["status"] in ("done", "review") and a["status"] not in a.get("protokolliert", []):
            try:
                detail = _hermes_json("kanban", "show", a["id"], timeout=60)
            except HTTPException:
                detail = {}
            kommentare = detail.get("comments") or []
            letzter = (kommentare[-1].get("body") or kommentare[-1].get("text") or "") if kommentare else ""
            bp.schreiben(bp.pfad_fuer(mandant), ereignis="notiz", werkzeug="trove-plugin/kanban", mandant=mandant, lauf=a["id"],
                         meldung="Task result (%s): %s" % (a["status"], (a["zusammenfassung"] or letzter or "(no text)")[:1500]), karte=a["id"],
                         kommentare=len(kommentare), ergebnis_kommentar=letzter[:1500])
            a.setdefault("protokolliert", []).append(a["status"]); a["ergebnis_kommentar"] = letzter[:1500]; geaendert = True
    if geaendert:
        _auftraege_speichern(mandant, liste)
    return {"auftraege": mcp + list(reversed(liste))}


# ---------- F3/F4/F6: Lesen, Verproben, Stapel zerlegen (alles lokal, keine bezahlten Token) ----------
FITZ_SKRIPTE = ("lese_treppe.py", "stapel.py", "wissen.py", "pdf_seite.py")  # brauchen pypdfium2/pikepdf; die anderen laufen in jedem Python
OHNE_FITZ_MOEGLICH = ("wissen.py",)  # laeuft notfalls ohne pypdfium2: Text/Markdown/Word gelesen, PDFs mit klarer Meldung


def _werkzeug(skript, *args, timeout=1800, ok_rc=(0,)):
    """OCR/PDF laufen im System-Python (pypdfium2, pikepdf, rapidocr) als Unterprozess — der Hermes-Server hat ein eigenes Python ohne
    diese Pakete. ok_rc: Rueckgabewerte, bei denen die letzte Zeile die JSON-Zusammenfassung ist (wissen: 2 = aborted, a run is already active)."""
    try:
        python = konfig.python_finden("pypdfium2" if skript in FITZ_SKRIPTE else None)
    except RuntimeError as ex:
        if skript not in OHNE_FITZ_MOEGLICH:
            raise _fehler(500, str(ex))
        try:
            python = konfig.python_finden(None)
        except RuntimeError as ex2:
            raise _fehler(500, str(ex2))
    p = subprocess.Popen([python, os.path.join(TROVE_REPO, skript), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace", cwd=TROVE_REPO,
                         env=konfig.kind_env(PYTHONUTF8="1", PYTHONIOENCODING="utf-8",  # Windows-Codepage: Umlaute sauber
                                             **({"TROVE_AGENT": "trove:ins-wissen"} if skript == "wissen.py" else {})))
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":  # Enkel (qmd/node) haengen sonst weiter: nur /T erreicht den ganzen Baum
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            p.kill()
        try:
            p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        raise HTTPException(504, skript + ": timed out, process tree terminated")
    out, err = out or "", err or ""
    if p.returncode not in ok_rc:
        raise _fehler(500, "%s fehlgeschlagen (rc %d): %s" % (skript, p.returncode, (err or out)[-800:]))
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise _fehler(500, skript + ": no JSON output: " + out[-400:] + (" | stderr: " + err[-400:] if err else ""))




def _quell_datei(mandant, body):
    q = _quelle(mandant, body.get("quelle", ""))
    voll = _sicher(q["pfad"], body.get("pfad", ""))
    if not os.path.isfile(voll):
        raise HTTPException(404, "File not found: " + body.get("pfad", ""))
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
    r"""F6: mehrseitiges PDF in Belege zerlegen (Stirling-PDF), Ergebnis unter <mandant>\stapel\<sha12>\, F8 'zerlegt' je Beleg."""
    mandant = body.get("mandant") or MANDANT
    voll = _quell_datei(mandant, body)
    r = _werkzeug("stapel.py", voll, "--mandant", mandant, "--json", ok_rc=(0, 3))
    if r.get("fehler"):
        raise HTTPException(503, r["fehler"])
    return {"ok": True, **{k: v for k, v in r.items() if k != "seiten_text"}}


















# ---------- F15: eine Suche ueber beide Welten ----------
@router.get("/suche")
def suche(q: str, mandant: str = MANDANT, n: int = 30):
    import suche as su
    _mandant_pruefen(mandant)
    return su.suchen(q, mandant, max(5, min(int(n), 100)))


# ---------- F16: Ordner ins Wissen (Markdown-Zwilling, QMD-Sammlung, Beleg-Karten privat/oeffentlich) ----------
SAMMLUNG_STANDARD = "quellen"  # wie wissen.SAMMLUNG_STANDARD (hier nicht importiert: wissen.py laeuft als Unterprozess)
ZUG_OFFEN = "offen"  # wie wissen.ZUG_OFFEN
def _sammlung(mandant, body):
    import gemeinsam as gm  # F16 erst in der Route laden
    sammlung = (body.get("sammlung") or "").strip().lower() or SAMMLUNG_STANDARD  # keine Pflicht: leer -> quellen
    try:
        gm.pruefe_namen(mandant, sammlung)
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    return sammlung


def _max(body):
    if body.get("max") is None:
        return []
    try:
        if isinstance(body["max"], bool):
            raise TypeError
        n = int(body["max"])
    except (TypeError, ValueError):
        raise HTTPException(400, "max: whole number")
    if n < 1:
        raise HTTPException(400, "max: at least 1")  # 0 hiesse im Werkzeug "alle"
    return ["--max", str(n)]


@router.get("/wissen/sammlungen")
def wissen_sammlungen(mandant: str = MANDANT):
    _mandant_pruefen(mandant)
    w = os.path.join(TROVE_DATEN, mandant, "wissen")
    out = []
    if os.path.isdir(w):
        for n in sorted(os.listdir(w)):
            d = os.path.join(w, n)
            if os.path.isdir(d):
                out.append({"sammlung": n, "qmd": "trove-%s-%s" % (mandant, n),
                            "markdown": sum(1 for f in os.listdir(d) if f.endswith(".md")), "ordner": d})
    return {"sammlungen": out}


@router.post("/wissen")
def wissen(body: dict):
    """Into knowledge: read the files of a source folder -> one markdown twin per file (bound to the original) -> optional knowledge cards in the wiki -> optional qmd index.
    sichtbarkeit privat = own use, oeffentlich = card limited to the public allow-list. Runs as a subprocess in the system Python."""
    mandant = _mandant_pruefen(body.get("mandant") or MANDANT)
    q = _quelle(mandant, body.get("quelle", ""))
    wurzel = _sicher(q["pfad"], body.get("pfad", ""))
    if not os.path.isdir(wurzel):
        raise HTTPException(404, "Folder not found")
    sicht = body.get("sichtbarkeit") or "privat"  # no mandatory fields: default private
    if sicht not in ("privat", "oeffentlich"):
        raise HTTPException(400, "sichtbarkeit: privat (private) | oeffentlich (public)")
    sammlung = _sammlung(mandant, body)
    if body.get("zugehoerigkeit") is not None and not isinstance(body.get("zugehoerigkeit"), str):
        raise HTTPException(400, "zugehoerigkeit: text")
    zug = (body.get("zugehoerigkeit") or "").strip() or ZUG_OFFEN  # leer -> "offen"; die Belegbuchung fragt spaeter nach
    auswahl = body.get("dateien") or []
    if not isinstance(auswahl, list) or not all(isinstance(x, str) and x.strip() for x in auswahl) or len(auswahl) > 500:
        raise HTTPException(400, "dateien: list of relative paths (at most 500)")
    einzeln = []
    for rel in auswahl:
        voll = _sicher(q["pfad"], rel)  # realpath-Pruefung: nie ausserhalb der eingebundenen Quelle
        if not _unter_real(voll, wurzel) or not os.path.isfile(voll):
            raise HTTPException(400, "no such file in the chosen folder: %s" % rel)
        einzeln.append(voll)
    args = [wurzel, "--mandant", mandant, "--sammlung", sammlung, "--sichtbarkeit", sicht,
            "--zugehoerigkeit=" + zug, "--json", *_max(body)]
    if body.get("traeger"):
        args.append("--traeger=" + str(body["traeger"]))
    if body.get("karten"):
        args.append("--karten")
    if body.get("ohne_embed"):
        args.append("--ohne-embed")
    args += ["--datei=" + p for p in einzeln]
    r = _werkzeug("wissen.py", *args, timeout=7200, ok_rc=(0, 2))
    if r.get("lauf_laeuft"):
        raise _fehler(409, r.get("abbruch") or "A run is already active")
    # Remember the folder for the optional continuous run (dauerlauf.py, needs the OS scheduler job from tools/installieren.py) -
    # only when the caller asks for it explicitly; never the knowledge folder itself, never a hand-picked file selection.
    folgen = body.get("nachfuehren") is True and not r.get("eigene_ablage") and not einzeln
    if folgen:
        konfig.ueberwacht_eintragen({"ordner": wurzel, "sammlung": sammlung, "sichtbarkeit": sicht, "zugehoerigkeit": zug,
                                     "traeger": body.get("traeger") or "", "karten": bool(body.get("karten"))})
    return {"ok": True, "nachgefuehrt": folgen, **{k: v for k, v in r.items() if k != "eintraege"}, "eintraege": r.get("eintraege", [])[:200]}


