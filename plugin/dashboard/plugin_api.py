"""Trove — Backend des Hermes-Desktop-Tabs. Mount: /api/plugins/reraise-trove/

Sources (local folders), two panes (list + preview), actions copy / move / sync. Every action goes through the
receipt log (F8) with a read-back check (sha256 of the file as written to disk). Duplicates via the register (F1, SQLite per client).
Nothing is overwritten. Writing is per source folder: a source is read-only unless it was explicitly added as writable.
Copy needs a writable target; move additionally deletes the source file after the read-back check, so it needs a writable source too.
Optional integrations (mail, DocuSeal, ERPNext, Reraise) are inactive until configured in Hermes; their credentials come only from Hermes (zugang.py).
"""
from __future__ import annotations

import base64
import errno
import functools
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

from fastapi import APIRouter, HTTPException, Request

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
VERSION = "0.3.0"


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


def _geschwaerzt(text):
    """Secret values read by the optional integrations (zugang.py) masked; unchanged when no integration is loaded."""
    z = sys.modules.get("zugang")
    return z.schwaerzen(text) if z is not None else text


def _aussen(text):
    """Every error text that leaves the process (HTTP response, MCP client, MCP queue): secrets masked, known paths stripped."""
    return _ohne_pfade(_geschwaerzt(str(text)))


def _fehler(code, text):
    """HTTP-Fehler ohne bekannte Pfade und ohne Geheimwerte nach aussen; im Server-Log steht der Text ohne Geheimwerte."""
    text = _geschwaerzt(str(text))
    _LOG.warning("trove HTTP %s: %s", code, text)
    return HTTPException(code, _ohne_pfade(text))


def _fehlertexte_nach_aussen(fn):
    """Endpoint wrapper: every HTTPException detail it raises passes _aussen (secret masking + path stripping),
    also when the function is called directly (MCP queue release)."""
    @functools.wraps(fn)
    def huelle(*a, **k):
        try:
            return fn(*a, **k)
        except HTTPException as ex:
            if isinstance(ex.detail, str):
                raise HTTPException(ex.status_code, _aussen(ex.detail), headers=getattr(ex, "headers", None)) from None
            raise
    return huelle










def _unter_real(pfad, wurzel):
    """Liegt pfad nach Aufloesen aller Verknuepfungen (realpath) unter der aufgeloesten wurzel?"""
    r, w = os.path.normcase(os.path.realpath(pfad)), os.path.normcase(os.path.realpath(wurzel))
    try:
        return os.path.commonpath([w, r]) == w
    except ValueError:  # anderes Laufwerk
        return False


def _gleich(a, b):
    """Do a and b name the same file or folder (same inode/device, or same resolved path)?"""
    try:
        if os.path.exists(a) and os.path.exists(b) and os.path.samefile(a, b):
            return True
    except OSError:
        pass
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _selbst_ziel(start, ziel, nach_ordner):
    """Reason text if copying/moving start to ziel would act on itself (same entry, or target folder inside the folder), else None."""
    if not os.path.lexists(start):
        return None
    if _gleich(start, ziel):
        return "Source and target are the same entry: %s." % os.path.basename(start.rstrip("/\\"))
    if os.path.isdir(start) and not ist_verknuepfung(start) and _unter_real(nach_ordner, start):
        return "The target folder is inside the folder being copied or moved: %s." % os.path.basename(start.rstrip("/\\"))
    return None


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
            dirs[:] = [d for d in dirs if not d.startswith(".") and not ist_verknuepfung(os.path.join(wurzel, d))]
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
    if os.path.lexists(nach) and _gleich(von, nach):  # never "already there" for the file itself: a move would delete the only copy
        prot.schreiben("fehler", quelle=von, ziel=nach, werkzeug=werkzeug, mandant=mandant, lauf=lauf, meldung="refused: source and target are the same file, nothing copied or deleted")
        return "fehler"
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


def _dateien_unter(voll, links=None):
    """Regular files under voll. Links are never followed and never copied: a link (to a file or a folder, also one pointing
    back up into the same tree) is only reported in `links`, so a copy can neither recurse nor act on the folder a link points to."""
    if ist_verknuepfung(voll):
        if links is not None:
            links.append(voll)
        return
    if os.path.isfile(voll):
        yield voll, os.path.basename(voll)
        return
    for wurzel, dirs, files in os.walk(voll, followlinks=False):
        for d in [d for d in dirs if ist_verknuepfung(os.path.join(wurzel, d))]:
            dirs.remove(d)
            if links is not None:
                links.append(os.path.join(wurzel, d))
        for fn in files:
            p = os.path.join(wurzel, fn)
            if ist_verknuepfung(p):
                if links is not None:
                    links.append(p)
                continue
            yield p, os.path.relpath(p, os.path.dirname(voll))


FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_REPARSE_TAG_NAME_SURROGATE = 0x20000000  # set for symlinks, junctions (mount points), WSL symlinks; not for OneDrive/cloud or dedup files


def ist_verknuepfung(p):
    """THE link check for every place that must never follow a link: symlinks, NTFS junctions (os.path.isjunction, Python 3.12+),
    and any Windows reparse point that redirects to another path (lstat: FILE_ATTRIBUTE_REPARSE_POINT with a name-surrogate tag,
    also on older Python; a reparse point without a known tag counts as a link). OneDrive placeholders and dedup files are
    ordinary files and folders."""
    try:
        if os.path.islink(p):
            return True
        ij = getattr(os.path, "isjunction", None)
        if ij is not None and ij(p):
            return True
        st = os.lstat(p)
        if getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
            tag = getattr(st, "st_reparse_tag", None)
            return tag is None or bool(tag & _REPARSE_TAG_NAME_SURROGATE)
    except (OSError, ValueError):
        return False
    return False


def _ueber_link(wurzel, voll):
    """First folder/entry between the source root and voll (inclusive) that is a link, else None."""
    rel = os.path.relpath(voll, wurzel)
    if rel in (".", ""):
        return None
    p = wurzel
    for teil in rel.split(os.sep):
        p = os.path.join(p, teil)
        if ist_verknuepfung(p):
            return p
    return None


def _kein_link_im_pfad(wurzel, ordner):
    """Rename / new folder never act through a link (symlink or junction) between the source root and the folder."""
    link = _ueber_link(wurzel, ordner)
    if link:
        raise HTTPException(409, "The path goes through a link (%s). Trove does not follow links. Nothing was changed." % os.path.basename(link))


def _ziel_sicher(wurzel, ziel):
    """Reason text if writing the file ziel would pass through a link (B3): any existing component between the target source root
    and ziel (ziel itself included) is a link/junction, or the nearest existing folder resolves outside the target source. Else None."""
    link = _ueber_link(wurzel, ziel)
    if link:
        return "target path goes through a link: %s" % os.path.relpath(link, wurzel)
    da = os.path.dirname(ziel)
    while not os.path.lexists(da) and os.path.dirname(da) != da:
        da = os.path.dirname(da)
    if not _unter_real(da, wurzel):
        return "target folder resolves outside the target source"
    return None


@router.post("/aktion")
@_fehlertexte_nach_aussen
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
            prot.schreiben("fehler", quelle=von_ordner, ziel=nach_ordner, werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="Action %s refused: target is inside the source folder" % art)
            raise HTTPException(400, "Target is inside the source folder. Nothing was copied, moved or deleted.")
        namen = body.get("namen") or ([os.path.basename(von_ordner)] if art != "sync" else [None])
        for n in namen:
            if n and os.path.lexists(os.path.join(von_ordner, n)) and not _unter_real(os.path.join(von_ordner, n), von_ordner):
                prot.schreiben("fehler", quelle=os.path.join(von_ordner, n), werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="Action %s refused: link leaves the source folder" % art)
                raise HTTPException(400, "Path leaves the source folder (symlink): %s" % n)
        for wurzel, ordner in ((qv["pfad"], von_ordner), (qn["pfad"], nach_ordner)):  # links are never followed (B2): no path through one
            link = _ueber_link(wurzel, ordner)
            if link:
                prot.schreiben("fehler", quelle=link, werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="Action %s refused: path goes through a link" % art)
                raise HTTPException(409, "The path goes through a link (%s). Trove does not follow links. Nothing was copied, moved or deleted." % os.path.basename(link))
        for n in namen:
            start = os.path.join(von_ordner, n) if n else von_ordner
            if n and ist_verknuepfung(start):
                prot.schreiben("fehler", quelle=start, werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="Action %s refused: entry is a link" % art)
                raise HTTPException(409, "%s is a link. Trove does not copy, move or sync links (that would act on what the link points to). "
                                         "Nothing was copied, moved or deleted." % n)
        for n in namen:  # B3: the entry's counterpart in the target must not be a link (writing would go through it)
            grund = _ziel_sicher(qn["pfad"], os.path.join(nach_ordner, n)) if n else None
            if grund:
                prot.schreiben("fehler", ziel=os.path.join(nach_ordner, n), werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="Action %s refused: %s" % (art, grund))
                raise HTTPException(409, "In the target, %s. Trove does not write through links. Nothing was copied, moved or deleted." % grund)
        for n in namen:  # copy/move/sync onto itself: refuse before anything is touched (a move would delete the only copy)
            start = os.path.join(von_ordner, n) if n else von_ordner
            grund = _selbst_ziel(start, os.path.join(nach_ordner, n) if n else nach_ordner, nach_ordner)
            if grund:
                prot.schreiben("fehler", quelle=start, ziel=nach_ordner, werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="Action %s refused: %s" % (art, grund))
                raise HTTPException(409, grund + " Nothing was copied, moved or deleted.")
        for n in namen:
            start = os.path.join(von_ordner, n) if n else von_ordner
            if not os.path.exists(start):
                ergebnis.append({"name": n, "status": "fehlt"}); continue
            zusammen = {"verifiziert": 0, "schon_da": 0, "konflikt": 0, "fehler": 0}
            if os.path.isdir(start) and not ist_verknuepfung(start):  # folders (also empty ones) exist at the target before any source is removed
                for wurzel, dirs, _files in os.walk(start):
                    dirs[:] = [d for d in dirs if not ist_verknuepfung(os.path.join(wurzel, d))]
                    rel_d = os.path.relpath(wurzel, os.path.dirname(start)) if n else os.path.relpath(wurzel, von_ordner)
                    if rel_d == ".":
                        continue
                    ziel_d = os.path.join(nach_ordner, rel_d)
                    grund = _ziel_sicher(qn["pfad"], ziel_d)  # B3: no folder is created through a link inside the target
                    if grund:  # counted as a conflict (the files below are counted one by one); a move keeps the source folder
                        zusammen["konflikt"] += 1
                        prot.schreiben("fehler", quelle=wurzel, ziel=ziel_d, werkzeug="trove-plugin", mandant=mandant, lauf=lauf,
                                       meldung="folder not created (%s); source kept" % grund)
                        dirs[:] = []
                        continue
                    if os.path.isdir(ziel_d):
                        continue
                    if os.path.lexists(ziel_d):
                        zusammen["konflikt"] += 1
                        prot.schreiben("fehler", quelle=wurzel, ziel=ziel_d, werkzeug="trove-plugin", mandant=mandant, lauf=lauf,
                                       meldung="folder not created: another entry with this name exists at the target; source kept")
                        continue
                    try:
                        os.makedirs(ziel_d)
                    except OSError as ex:
                        zusammen["fehler"] += 1
                        prot.schreiben("fehler", quelle=wurzel, ziel=ziel_d, werkzeug="trove-plugin", mandant=mandant, lauf=lauf,
                                       meldung="folder not created: %s; source kept" % (ex.strerror or type(ex).__name__))
                        continue
                    prot.schreiben("notiz", quelle=wurzel, ziel=ziel_d, werkzeug="trove-plugin", mandant=mandant, lauf=lauf,
                                   meldung="folder created at the target: %s" % rel_d)
            links = []
            for p, rel in _dateien_unter(start, links):
                ziel_pfad = os.path.join(nach_ordner, rel) if n else os.path.join(nach_ordner, os.path.relpath(p, von_ordner))
                grund = _ziel_sicher(qn["pfad"], ziel_pfad)  # B3: checked for every file, before any folder is created
                if grund:
                    prot.schreiben("fehler", quelle=p, ziel=ziel_pfad, werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="not written (%s); source kept" % grund)
                    zusammen["fehler"] += 1
                    continue
                st = _kopiere_datei(prot, mandant, lauf, p, ziel_pfad)
                zusammen[st] = zusammen.get(st, 0) + 1
                if art == "verschieben" and st in ("verifiziert", "schon_da"):
                    os.remove(p)
                    prot.schreiben("verschoben", quelle=p, ziel=ziel_pfad, sha256=_sha(ziel_pfad), bytes_=os.path.getsize(ziel_pfad), werkzeug="trove-plugin",
                                   mandant=mandant, lauf=lauf, meldung="source removed after the read-back check passed")
            for link in links:
                prot.schreiben("notiz", quelle=link, werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="link skipped: Trove does not copy or move links")
            if links:
                zusammen["links_skipped"] = len(links)
            if art == "verschieben" and os.path.isdir(start) and zusammen["konflikt"] == 0 and zusammen["fehler"] == 0:
                ordner_liste = []
                for wurzel, dirs, _files in os.walk(start):  # never descend into a link or junction
                    dirs[:] = [d for d in dirs if not ist_verknuepfung(os.path.join(wurzel, d))]
                    ordner_liste.append(wurzel)
                for wurzel in reversed(ordner_liste):
                    if not os.listdir(wurzel):
                        os.rmdir(wurzel)
            ergebnis.append({"name": n or "(folder)", **zusammen})
        prot.schreiben("notiz", werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="Action %s finished: %s" % (art, json.dumps(ergebnis, ensure_ascii=False)[:800]))
        prot.anker_setzen(mandant, lauf)
    return {"ok": True, "lauf": lauf, "ergebnis": ergebnis}


_WIN_RESERVIERT = {"CON", "PRN", "AUX", "NUL", "CLOCK$", *("COM%d" % i for i in range(1, 10)), *("LPT%d" % i for i in range(1, 10))}
_NAME_VERBOTEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _eintragsname_pruefen(name, was="name"):
    """Einen neuen Datei-/Ordnernamen pruefen (rename / new folder): ein einzelner Name, kein Pfad, auf allen Systemen anlegbar."""
    if not isinstance(name, str) or not name:
        raise HTTPException(400, "%s: a name is required" % was)
    if name != name.strip(" ") or name.endswith("."):
        raise HTTPException(400, "%s: must not start or end with a space or end with a dot" % was)
    if name in (".", "..") or _NAME_VERBOTEN.search(name) or os.path.isabs(name) or os.path.splitdrive(name)[0]:
        raise HTTPException(400, "%s: no path separators, '..' or characters that are not allowed in file names: %r" % (was, name))
    try:
        laenge = len(name.encode("utf-8"))
    except UnicodeEncodeError:
        raise HTTPException(400, "%s: not valid text (lone surrogate characters)" % was)
    if laenge > 255:
        raise HTTPException(400, "%s: too long" % was)
    if name.split(".")[0].rstrip(" ").upper() in _WIN_RESERVIERT:
        raise HTTPException(400, "%s: reserved system name: %r" % (was, name))
    return name


def _ordnerpfad_pruefen(pfad):
    if ".." in str(pfad or "").replace("\\", "/").split("/"):
        raise HTTPException(400, "pfad: '..' is not allowed")
    return pfad


def _prot_oeffnen(mandant):
    """Receipt log for a click action: wait up to ~10 s if another run holds it, then answer 503 (nothing was changed yet)."""
    try:
        return gm.prot_mit_wiederholung(bp.pfad_fuer(mandant), versuche=20, pause=0.5)
    except bp.ProtokollGesperrt:
        raise HTTPException(503, "The receipt log is busy right now (another run is writing to it). Nothing was changed. Try again in a moment.")


def _gleicher_eintrag(ordner, name, neu):
    """True only if name and neu are ONE directory entry that the filesystem treats as equal (case-insensitive filesystem, case-only rename).
    Two separate entries (symlink or hard-link twins on a case-sensitive filesystem) are never the same entry."""
    try:
        a, b = os.lstat(os.path.join(ordner, name)), os.lstat(os.path.join(ordner, neu))
    except OSError:
        return False
    if (a.st_ino, a.st_dev) != (b.st_ino, b.st_dev):
        return False
    vorhanden = os.listdir(ordner)
    return name in vorhanden and neu not in vorhanden


def _wird_benutzt(mandant, ordner, name):
    """Does renaming ordner/name break a registered source or a folder watched by 'Into knowledge'? Returns a short description or None."""
    alt = os.path.join(ordner, name)
    varianten = {os.path.normcase(os.path.join(os.path.realpath(ordner), name)), os.path.normcase(os.path.abspath(alt))}

    def innerhalb(pfad):
        if not pfad:
            return False
        for v in {os.path.normcase(os.path.realpath(pfad)), os.path.normcase(os.path.abspath(pfad))}:
            for a in varianten:
                if v == a or v.startswith(a + os.sep):
                    return True
        return False
    for x in _quellen(mandant):
        if innerhalb(x.get("pfad")):
            nm = str(x.get("name") or x.get("pfad") or "?")
            return "a source (%s)" % (os.path.basename(nm.rstrip("/\\")) or nm)  # display name only, never the full path
    try:
        watched = konfig.lesen().get("ueberwacht", [])
    except (OSError, ValueError):
        watched = []
    for w in watched:
        if innerhalb(w.get("ordner")):
            return "a folder watched by 'Into knowledge'"
    return None


def _schreibbare_quelle(mandant, qid):
    q = _quelle(mandant, qid)
    if q.get("nur_lesen", True):
        raise HTTPException(403, "This source is read-only. Mark the folder as writable to rename or create folders in it.")
    return q


def _umbenennen_ohne_ueberschreiben(alt, ziel, gleiche_datei=False):
    """Rename alt -> ziel and NEVER replace an existing ziel, even if it appears after the caller's check. Raises FileExistsError.
    Windows: os.rename already refuses an existing target. POSIX files: hard link (atomic, fails if ziel exists) then unlink the old name.
    Linux folders: renameat2(RENAME_NOREPLACE). Only if the filesystem or libc lacks these does it fall back to check + os.rename
    (a tiny window remains there). Case-only renames of the same entry use a plain rename. Returns the method used."""
    if os.name == "nt" or gleiche_datei:
        os.rename(alt, ziel)
        return "rename"
    if not os.path.isdir(alt) or ist_verknuepfung(alt):
        try:
            os.link(alt, ziel, follow_symlinks=False)
        except FileExistsError:
            raise
        except (OSError, NotImplementedError):
            pass  # no hard links here (EXDEV, EPERM, ENOTSUP ...): fall back below
        else:
            try:
                os.unlink(alt)
            except OSError:
                try:
                    os.unlink(ziel)  # undo, so the entry is never present under two names
                except OSError:
                    pass
                raise
            return "link"
    elif sys.platform.startswith("linux"):
        err = None
        try:
            import ctypes
            fn = ctypes.CDLL(None, use_errno=True).renameat2
            fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            fn.restype = ctypes.c_int
            if fn(-100, os.fsencode(alt), -100, os.fsencode(ziel), 1) == 0:  # AT_FDCWD, RENAME_NOREPLACE
                return "renameat2"
            err = ctypes.get_errno()
        except (AttributeError, OSError):
            err = errno.ENOSYS  # libc without renameat2: fall back below
        if err in (errno.EEXIST, errno.ENOTEMPTY):
            raise FileExistsError(err, os.strerror(err), ziel)
        if err not in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM):
            raise OSError(err, os.strerror(err), alt)
    if os.path.lexists(ziel):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), ziel)
    os.rename(alt, ziel)
    return "check+rename"


@router.post("/umbenennen")
@_fehlertexte_nach_aussen
def umbenennen(body: dict):
    """Rename one file or folder inside a writable source. body: {quelle, pfad (the folder it is in), name, neu}. Never overwrites."""
    mandant = body.get("mandant") or MANDANT
    q = _schreibbare_quelle(mandant, body.get("quelle"))
    name, neu = body.get("name"), body.get("neu")
    _namen_pruefen([name])
    if not name:
        raise HTTPException(400, "name: the entry to rename is required")
    _eintragsname_pruefen(neu, "neu")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise HTTPException(400, "name: not valid text (lone surrogate characters)")
    ordner = _sicher(q["pfad"], _ordnerpfad_pruefen(body.get("pfad", "")))
    _kein_link_im_pfad(q["pfad"], ordner)
    alt, ziel = os.path.join(ordner, name), os.path.join(ordner, neu)
    if not os.path.lexists(alt):
        raise HTTPException(404, "Entry not found: %s" % name)
    if not _unter_real(ordner, q["pfad"]):
        raise HTTPException(400, "Path is outside the source folder")
    if name == neu:
        raise HTTPException(400, "The new name is the same as the old one")
    nur_gross_klein = name.casefold() == neu.casefold() and _gleicher_eintrag(ordner, name, neu)
    if os.path.lexists(ziel) and not nur_gross_klein:
        raise HTTPException(409, "An entry named %r already exists here. Nothing was renamed." % neu)
    benutzt = _wird_benutzt(mandant, ordner, name)
    if benutzt:
        raise _fehler(409, "%r is used as %s. Renaming it would break that. Remove it there first, then rename." % (name, benutzt))
    sha, groesse = "", None
    if os.path.isfile(alt) and not ist_verknuepfung(alt):
        groesse = os.path.getsize(alt)
        if groesse <= 256 * 1024 * 1024:
            sha = _sha(alt)
    lauf = "umbenennen-%s" % time.strftime("%Y%m%d-%H%M%S")
    with _prot_oeffnen(mandant) as prot:
        try:
            _umbenennen_ohne_ueberschreiben(alt, ziel, nur_gross_klein)
        except FileExistsError:
            prot.schreiben("fehler", quelle=alt, ziel=ziel, werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="rename refused: target exists, nothing overwritten")
            raise HTTPException(409, "An entry named %r already exists here. Nothing was renamed." % neu)
        except OSError as ex:
            prot.schreiben("fehler", quelle=alt, ziel=ziel, werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="rename failed: %s" % (ex.strerror or type(ex).__name__))
            raise _fehler(500, "Rename failed: %s" % (ex.strerror or type(ex).__name__))
        vorhanden = os.listdir(ordner)
        if neu not in vorhanden or name in vorhanden:
            prot.schreiben("fehler", quelle=alt, ziel=ziel, werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="rename did not take effect, nothing logged as renamed")
            raise _fehler(500, "Rename did not take effect.")
        prot.schreiben("umbenannt", quelle=alt, ziel=ziel, sha256=sha, bytes_=groesse, werkzeug="trove-plugin", mandant=mandant, lauf=lauf,
                       meldung="%s renamed in place: %s -> %s" % ("folder" if os.path.isdir(ziel) and not ist_verknuepfung(ziel) else "file", name, neu))
        prot.anker_setzen(mandant, lauf)
    return {"ok": True, "lauf": lauf, "name": neu}


@router.post("/ordner-neu")
@_fehlertexte_nach_aussen
def ordner_neu(body: dict):
    """Create one new folder inside a writable source. body: {quelle, pfad (parent folder), name}. Never touches an existing entry."""
    mandant = body.get("mandant") or MANDANT
    q = _schreibbare_quelle(mandant, body.get("quelle"))
    name = _eintragsname_pruefen(body.get("name"), "name")
    ordner = _sicher(q["pfad"], _ordnerpfad_pruefen(body.get("pfad", "")))
    if not os.path.isdir(ordner):
        raise HTTPException(404, "Folder not found")
    _kein_link_im_pfad(q["pfad"], ordner)
    ziel = os.path.join(ordner, name)
    lauf = "ordner-neu-%s" % time.strftime("%Y%m%d-%H%M%S")
    with _prot_oeffnen(mandant) as prot:
        try:
            os.mkdir(ziel)  # atomic: fails if anything with that name exists
        except FileExistsError:
            prot.schreiben("fehler", quelle=ordner, ziel=ziel, werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="new folder refused: target exists, nothing touched")
            raise HTTPException(409, "An entry named %r already exists here. No folder was created." % name)
        except OSError as ex:
            prot.schreiben("fehler", quelle=ordner, ziel=ziel, werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="new folder failed: %s" % (ex.strerror or type(ex).__name__))
            raise _fehler(500, "New folder failed: %s" % (ex.strerror or type(ex).__name__))
        prot.schreiben("notiz", quelle=ordner, ziel=ziel, werkzeug="trove-plugin", mandant=mandant, lauf=lauf, meldung="folder created: %s" % name)
        prot.anker_setzen(mandant, lauf)
    return {"ok": True, "lauf": lauf, "name": name}


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
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except OSError as ex:  # not a program (WinError 193), not executable, gone: a clear message, not a crash of the request
        raise HTTPException(502, "hermes CLI could not be started (%s): %s" % (exe, getattr(ex, "strerror", None) or ex))
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


def _menschenklick(request, body):
    """Release/archive of an MCP request: only a JSON request with {"confirm": true}, as the tab sends it through the Hermes
    desktop/dashboard (which also adds its session token). Refuses forms and other plain browser posts (no CORS preflight) and any
    request from a foreign web origin."""
    art = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if art != "application/json":
        raise HTTPException(415, "Send JSON (Content-Type: application/json)")
    if not isinstance(body, dict) or body.get("confirm") is not True:
        raise HTTPException(400, 'Confirmation missing: send {"confirm": true}')
    host = (request.headers.get("host") or "").strip().lower()
    name = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    erlaubte_hosts = {"127.0.0.1", "localhost", "::1"} | {h.strip().lower() for h in os.environ.get("TROVE_DASHBOARD_HOSTS", "").split(",") if h.strip()}
    if name not in erlaubte_hosts:  # DNS rebinding: a foreign page reaches the dashboard under its own host name
        raise HTTPException(403, "Release only through the local dashboard (host %s is not allowed)" % (name or "missing"))
    origin = request.headers.get("origin")
    if origin is not None:
        eigen = "%s://%s" % (request.url.scheme, request.headers.get("host", ""))
        erlaubt = {eigen.lower()} | {o.strip().lower() for o in os.environ.get("TROVE_DASHBOARD_ORIGIN", "").split(",") if o.strip()}
        if origin.lower() not in erlaubt:
            raise HTTPException(403, "Foreign origin")


@router.post("/auftrag/{kid}/freigeben")
def auftrag_freigeben(kid: str, request: Request, body: dict = None):
    mandant = (body or {}).get("mandant") or MANDANT
    if mcp_queue.ist_mcp(kid):  # request from an MCP client: a human click here is its only way to run
        _menschenklick(request, body)
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
def auftrag_archivieren(kid: str, request: Request, body: dict = None):
    mandant = (body or {}).get("mandant") or MANDANT
    if mcp_queue.ist_mcp(kid):
        _menschenklick(request, body)
        try:
            return mcp_queue.ablehnen(_mandant_pruefen(mandant), kid)
        except KeyError:
            raise HTTPException(404, "Unknown task %s" % kid)
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
    import erkennung as ek  # optional recognition model (settings in Hermes; off = rules only)
    import zugang
    voll = _quell_datei(mandant, body)
    k = ek.konfiguration()
    args = [voll, "--mandant", mandant, "--ocr", body.get("ocr") or "rapidocr", "--max-seiten", str(int(body.get("max_seiten") or 10)), "--json"]
    if body.get("erzwinge_ocr"):
        args.append("--erzwinge-ocr")
    try:
        erg, ocr_bericht = ek.beleg_lesen(lambda extra: _werkzeug("lese_treppe.py", *args, *extra), mandant, k)
        bericht = ek.zwilling_erkennen(erg["zwilling"], k) if ek.aktiv(k) else None
    except ek.ErkennungFehler as ex:
        text = zugang.kurz(str(ex), 400)
        bp.schreiben(bp.pfad_fuer(mandant), ereignis="fehler", quelle=voll, werkzeug="erkennung.py", mandant=mandant,
                     meldung="recognition model: " + text, art=ex.art)
        raise _fehler(503, text)
    felder = erg["felder"]
    if bericht is not None or ocr_bericht is not None:
        with open(erg["zwilling"], encoding="utf-8") as f:
            felder = json.load(f).get("felder") or {}
    v = vp.verproben_zwilling(mandant, erg["zwilling"])
    out = {"ok": True, "zwilling": erg["zwilling"], "sha256": erg["sha256"], "methode": erg["methode"], "seiten": erg["seiten"], "dauer_s": round(erg["dauer_s"], 2),
           "felder": felder, "verprobung": {k: v[k] for k in ("bestanden", "sicher", "gruende", "regeln")}, "text_anfang": erg.get("text_anfang", ""),
           "read_by": ek.gelesen_von(bericht, ocr_bericht, erg["methode"])}
    if bericht is not None:
        out["erkennung"] = {k: bericht.get(k) for k in ("modus", "modell", "provider", "digest", "status", "grund", "dauer_s",
                                                         "felder_modell", "felder_regeln", "verworfen", "widerspruch")}
    if ocr_bericht is not None:
        out["ocr_modell"] = {k: ocr_bericht.get(k) for k in ("modell", "einsatz", "digest", "status", "grund", "sekunden", "s_je_seite", "seiten", "zweite_lesung")}
    return out


def _erk_lauf(f):
    """Recognition-model actions from the tab: not usable -> 503 with Trove's own text (masked); never an upstream body."""
    import erkennung as ek
    import zugang
    try:
        return f()
    except ek.ErkennungFehler as ex:
        raise _fehler(503, zugang.kurz(str(ex), 400))
    except Exception as ex:
        raise _fehler(502, zugang.fehlertext(ex, 300))


@router.get("/erkennung")
def erkennung_status():
    """Recognition model: mode, pinned model, problems. No network. Settings are changed only in Hermes (human)."""
    import erkennung as ek
    import zugang
    zugang.neu_laden()
    return ek.status()


@router.post("/erkennung/test")
def erkennung_test(body: dict = None):
    """'Test model' in the tab: built-in sample only (no receipt content); pins on the first successful test."""
    import erkennung as ek
    return _erk_lauf(ek.testen)


@router.post("/erkennung/pin")
def erkennung_pin(body: dict = None):
    """'Pin' in the tab: the current version of the configured model becomes the only one Trove uses. Needs confirm: true."""
    import erkennung as ek
    if not (body or {}).get("confirm") is True:
        raise HTTPException(400, "Pin needs confirm: true (the tab asks first)")
    return _erk_lauf(ek.pinnen)


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




# ---------- Optional integrations (mail, DocuSeal, ERPNext mirror, Reraise): inactive until configured in Hermes ----------
# Credentials only through Hermes (zugang.py: get_secret / oauth sign-in held by Hermes). No secret value ever appears in a
# response or a log line: every result and error passes zugang.schwaerzen().
RERAISE_LINK = "https://reraise.ai"
EINSTELLUNGEN_LINK = "/settings?tab=plugins&plugin=reraise-trove"


def _int_ergebnis(r):
    """Redact every value BEFORE serialising (JSON escaping would hide a secret with \\ or \"), then the text once more."""
    import zugang
    r = zugang.schwaerzen_tief(json.loads(json.dumps(r, ensure_ascii=False, default=str)))
    try:
        return json.loads(zugang.schwaerzen(json.dumps(r, ensure_ascii=False)))
    except ValueError:
        return r


def _int_lauf(f):
    """Run one integration call: not configured -> 403, run active -> 409, other errors -> 502; all texts masked."""
    import zugang
    try:
        return _int_ergebnis(f())
    except zugang.NichtEingerichtet as ex:
        raise _fehler(403, zugang.schwaerzen(ex))
    except bp.LaufLaeuft as ex:
        raise _fehler(409, zugang.schwaerzen(ex))
    except HTTPException:
        raise
    except (ValueError, KeyError) as ex:
        raise _fehler(400, zugang.schwaerzen(ex))
    except Exception as ex:
        raise _fehler(502, zugang.fehlertext(ex, 400))  # never an upstream body or reason phrase


@router.get("/integrationen")
def integrationen():
    """Which integrations are active. Names of missing settings only, never values. No network."""
    import zugang
    zugang.neu_laden()  # the UI's Refresh: settings may have changed in Hermes
    import docuseal
    import mail_abholer
    import reraise_hochladen
    import spiegel_erpnext
    ds, ds_fehlt = docuseal.eingerichtet()
    erp, erp_fehlt = spiegel_erpnext.eingerichtet()
    boxen = [{"id": p["id"], "kind": p["art"], "active": p["aktiv"], "missing": p["fehlt"], **({"signin": p["anmeldung"]} if "anmeldung" in p else {})}
             for p in mail_abholer.postfaecher()]
    return {"settings": EINSTELLUNGEN_LINK, "mail": {"mailboxes": boxen},
            "docuseal": {"active": ds, "missing": ds_fehlt}, "erpnext": {"active": erp, "missing": erp_fehlt},
            "reraise": {"active": reraise_hochladen.eingerichtet(), "link": RERAISE_LINK}, "erkennung": _erk_status()}


def _erk_status():
    import erkennung as ek
    try:
        return ek.status()
    except Exception as ex:  # never breaks the integrations overview
        return {"mode": "off", "problems": [type(ex).__name__]}


def _int_max(body, name="max"):
    v = body.get(name)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise HTTPException(400, name + ": whole number, at least 1")
    return v


@router.post("/integrationen/mail/abholen")
def int_mail_abholen(body: dict):
    import mail_abholer
    mandant = _mandant_pruefen(body.get("mandant") or MANDANT)
    pf = _int_lauf(lambda: mail_abholer.postfach(str(body.get("postfach") or "")))
    n = _int_max(body)
    return _int_lauf(lambda: {"ok": True, **mail_abholer.abholen(mandant, pf, n, body.get("nur_anhaenge") is True, pf.get("abfrage") or None)})


@router.get("/integrationen/docuseal/vorlagen")
def int_docuseal_vorlagen():
    import docuseal
    return _int_lauf(lambda: {"vorlagen": docuseal.vorlagen()})


def _bestaetigt(body):
    """Consequential actions need an explicit confirmation in the request itself (the UI sends it after the second click)."""
    if (body or {}).get("confirm") is not True:
        raise HTTPException(400, 'confirmation required: send {"confirm": true} after showing the user what will happen')


@router.post("/integrationen/docuseal/senden")
def int_docuseal_senden(body: dict):
    import docuseal
    _bestaetigt(body)
    mandant = _mandant_pruefen(body.get("mandant") or MANDANT)
    vorlage = body.get("vorlage")
    if isinstance(vorlage, bool) or not isinstance(vorlage, int):
        raise HTTPException(400, "vorlage: template id (number)")
    an = str(body.get("an") or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", an):
        raise HTTPException(400, "an: e-mail address of the signer")
    beleg = _quell_datei(mandant, body) if body.get("quelle") else None
    return _int_lauf(lambda: {"ok": True, **docuseal.senden(mandant, vorlage, an, str(body.get("name") or ""), None, beleg, body.get("mail") is True,
                                                           str(body.get("betreff") or "") or None)})


@router.post("/integrationen/docuseal/abholen")
def int_docuseal_abholen(body: dict = None):
    import docuseal
    mandant = _mandant_pruefen((body or {}).get("mandant") or MANDANT)
    return _int_lauf(lambda: {"ok": True, "vorgaenge": docuseal.abholen(mandant)})


@router.post("/integrationen/erpnext/doctypes")
def int_erpnext_doctypes(body: dict = None):
    import spiegel_erpnext
    _bestaetigt(body)
    return _int_lauf(lambda: {"ok": True, **spiegel_erpnext.doctypes_anlegen()})


@router.post("/integrationen/erpnext/spiegeln")
def int_erpnext_spiegeln(body: dict = None):
    import spiegel_erpnext
    body = body or {}
    mandant = _mandant_pruefen(body.get("mandant") or MANDANT)
    n = _int_max(body)

    def lauf():
        r = spiegel_erpnext.spiegeln(mandant, n)
        b = spiegel_erpnext.belege_aktualisieren(mandant)
        bp.schreiben(bp.pfad_fuer(mandant), ereignis="notiz", werkzeug="trove:erpnext", mandant=mandant,
                     meldung="ERPNext-Spiegel: %d Zeilen bis Zeile %d, %d Belege" % (r["gespiegelt"], r["letzte_zeile"], b["belege_aktualisiert"]))
        return {"ok": True, **r, **b}
    return _int_lauf(lauf)


@router.get("/integrationen/erpnext/historie")
def int_erpnext_historie(sha256: str):
    import spiegel_erpnext
    if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
        raise HTTPException(400, "sha256: 64 hex characters")
    return _int_lauf(lambda: spiegel_erpnext.historie(sha256))


def _reraise_vorbereiten(body):
    """Fail closed: without a Reraise key in Hermes nothing happens here (no preview, no upload, no network)."""
    import reraise_hochladen as rh
    if not rh.eingerichtet():
        raise _fehler(403, "Reraise is not configured: without a Reraise key in Hermes Trove only links to " + RERAISE_LINK)
    mandant = _mandant_pruefen(body.get("mandant") or MANDANT)
    q = _quelle(mandant, body.get("quelle", ""))
    pfad = _sicher(q["pfad"], body.get("pfad", ""))
    if not os.path.exists(pfad):
        raise HTTPException(404, "File or folder not found")
    return rh, mandant, pfad, _sammlung(mandant, body)


@router.post("/integrationen/reraise/probe")
def int_reraise_probe(body: dict):
    rh, mandant, pfad, sammlung = _reraise_vorbereiten(body)
    return _int_lauf(lambda: rh.hochladen(mandant, pfad, sammlung, _int_max(body), probe=True))


@router.post("/integrationen/reraise/senden")
def int_reraise_senden(body: dict):
    rh, mandant, pfad, sammlung = _reraise_vorbereiten(body)
    liste = body.get("bestaetigt")
    if not isinstance(liste, list) or not liste or not all(isinstance(x, str) for x in liste) or len(liste) > 200:
        raise HTTPException(400, "bestaetigt: list of 'sha256:card_hash' from the preview")
    nur = _int_lauf(lambda: rh.sha_liste(" ".join(liste)))
    return _int_lauf(lambda: rh.hochladen(mandant, pfad, sammlung, None, probe=False, nur=nur, schluessel=rh._schluessel()))
