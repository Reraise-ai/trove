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
# Module liegen installiert neben dieser Datei, im Repo zwei Ordner darueber; TROVE_REPO ueberschreibt
TROVE_REPO = next((d for d in (os.environ.get("TROVE_REPO"), HIER, os.path.normpath(os.path.join(HIER, "..", ".."))) if d and os.path.isfile(os.path.join(d, "belegprotokoll.py"))), HIER)
if TROVE_REPO not in sys.path:
    sys.path.insert(0, TROVE_REPO)
import konfig  # noqa: E402
import belegprotokoll as bp  # noqa: E402
import aenderungen  # noqa: E402
import gesundheit  # noqa: E402
import gemeinsam as gm  # noqa: E402

REGISTER_ALT = os.environ.get("TROVE_REGISTER_DB", "")  # optional: Altindex (nur intern); leer = aus
RCLONE = os.environ.get("TROVE_RCLONE") or shutil.which("rclone") or os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\WinGet\Links\rclone.exe")

TROVE_DATEN = bp.WURZEL  # eine Datenwurzel (Umgebungsvariable TROVE_DATEN, siehe belegprotokoll)
RE_MANDANT = r"[a-z0-9][a-z0-9_-]{0,40}"  # muss gleich gemeinsam.RE_MANDANT sein (Kopie: Plugin laedt ohne F16-Module)
router = APIRouter()
BLOCK = 1 << 20
MANDANT = konfig.mandant()  # trove.json / TROVE_MANDANT


def _jetzt():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _sha(pfad):
    h = hashlib.sha256()
    with open(pfad, "rb") as f:
        for b in iter(lambda: f.read(BLOCK), b""):
            h.update(b)
    return h.hexdigest()


def _mandant_pruefen(mandant):
    if not re.fullmatch(RE_MANDANT, mandant or ""):
        raise HTTPException(400, "mandant: a-z 0-9 _ - (1-41 Zeichen)")
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
        raise _fehler(500, "rclone nicht gefunden: " + RCLONE)
    r = subprocess.run([RCLONE, *args], capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise HTTPException(502, "rclone: " + (r.stderr.strip()[-400:] or "Fehler %d" % r.returncode))
    return r.stdout


def _kundenmodus():
    """Kundenmodus: die Ablage des Kunden wird nur gelesen; alles, was in eine Quelle schreibt, ist gesperrt (konfig.kartenziel:
    nur ein ausdrueckliches brett ist der interne Server)."""
    return konfig.kartenziel() == "wiki"


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


def _postfach_da():
    """Mindestens ein Postfach eingerichtet (postfaecher.json); der Tab zeigt "Mail abholen" im Kundenmodus nur dann."""
    import mail_abholer as ma
    try:
        return bool(ma.postfaecher(MANDANT))
    except (OSError, ValueError):
        return False


def _reraise_knopf():
    """Knopf "zu Reraise" (Upload oeffentlicher Karten zu reraise.ai): im Kundenmodus nur auf ausdruecklichen Wunsch (trove.json reraise_knopf = 1), sonst aus."""
    return (not _kundenmodus()) or os.environ.get("TROVE_RERAISE_KNOPF", "").strip().lower() in ("1", "true", "an")  # trove.json true -> "True"


ICLOUD_DRIVE = os.path.join("~", "Library", "Mobile Documents", "com~apple~CloudDocs")


def _icloud_drive():
    """Lokaler iCloud-Drive-Ordner (Mac, iCloud Drive eingeschaltet) oder None. Kundenmodus: einziger Cloud-Weg (rclone fehlt dort)."""
    p = os.path.expanduser(ICLOUD_DRIVE)
    return p if os.path.isdir(p) else None


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
        raise HTTPException(400, "Pfad ausserhalb der Quelle")
    return voll


def _namen_pruefen(namen):
    """namen aus /aktion: nur Eintragsnamen im gewaehlten Ordner (Basename), keine Pfade, kein .. und nichts Absolutes."""
    if not isinstance(namen, list):
        raise HTTPException(400, "namen: Liste von Eintragsnamen")
    for n in namen:
        if n in (None, ""):
            continue
        if (not isinstance(n, str) or n in (".", "..") or "/" in n or "\\" in n or "\0" in n or os.path.isabs(n)
                or os.path.splitdrive(n)[0] or os.path.basename(n) != n):
            raise HTTPException(400, "namen: nur Eintragsnamen im Ordner, keine Pfade: %r" % (n,))
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
    return {"ok": True, "mandant": MANDANT, "datenwurzel": TROVE_DATEN, "kartenziel": konfig.kartenziel(), "reraise_knopf": _reraise_knopf(), "postfach": _postfach_da(), "erpnext": bool(_sp().einstellung("TROVE_ERPNEXT")), "profile": gesundheit.hermes_profile(), "hermes_cli": _hermes_exe() is not None,
            "zeit": _jetzt(), "stufe": "F2 F3 F4 F5 F6 F7 F8 F9 F10 F11 F15 F16", "geraet": os.environ.get("COMPUTERNAME", ""),
            "pid": os.getpid(), "protokolle": prot, "register": reg, "rclone": os.path.exists(RCLONE), "icloud_drive": _icloud_drive(), "wissen_einzeln": True, "wissen_ohne_pflicht": True}


@router.get("/gesundheit")
def gesundheit_route():
    return gesundheit.bericht()


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
    if art == "auto":  # eingetippter Pfad: "name:unterordner" = rclone-Remote, sonst lokaler Ordner (Laufwerksbuchstabe C: zaehlt nicht)
        m = re.fullmatch(r"([A-Za-z0-9_][A-Za-z0-9_.-]+):(?![\\/]{2})(.*)", (body.get("pfad") or "").strip())
        if m:
            art, body = "rclone", {**body, "remote": m.group(1), "pfad": m.group(2)}
        else:
            art = "ordner"
    if art == "ordner":
        pfad = os.path.abspath(os.path.expanduser(body.get("pfad") or ""))
        if not os.path.isdir(pfad):
            raise HTTPException(400, "Ordner nicht gefunden: " + pfad)
        q = {"id": hashlib.sha1(("ordner:" + pfad.casefold()).encode()).hexdigest()[:10], "art": "ordner", "name": name or pfad, "pfad": pfad,
             "nur_lesen": True if _kundenmodus() else bool(body.get("nur_lesen", True))}  # Standard: nur lesen; beschreibbar nur ausdruecklich
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


CLOUD_TYPEN = {  # Anzeigename -> rclone-Backend und die Felder, die der Tab abfragt
    "webdav": ("webdav", ["url", "vendor", "user", "pass"]), "sftp": ("sftp", ["host", "port", "user", "pass"]),
    "mega": ("mega", ["user", "pass"]), "drive": ("drive", []), "icloud": ("iclouddrive", ["apple_id", "password"]),
}


@router.post("/cloud")
def cloud_anbinden(body: dict):
    """Cloud-Speicher ueber rclone anbinden (Remote anlegen, Zugang pruefen, bei Fehlschlag wieder entfernen).
    drive: rclone oeffnet auf diesem Rechner den Browser zur Anmeldung. Passwoerter gehen verschleiert in die rclone-Konfiguration."""
    typ, name = body.get("typ"), (body.get("name") or "").strip()
    if typ not in CLOUD_TYPEN:
        raise HTTPException(400, "typ: " + ", ".join(CLOUD_TYPEN))
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]{1,30}", name):
        raise HTTPException(400, "name: 2-31 Zeichen aus A-Z a-z 0-9 _ -")
    if name + ":" in [r.strip() for r in _rclone("listremotes", timeout=20).splitlines()]:
        raise HTTPException(409, "Remote %s gibt es schon" % name)
    backend, felder = CLOUD_TYPEN[typ]
    kv = []
    for k in felder:
        v = str((body.get("felder") or {}).get(k) or "").strip()
        if v:
            kv += [k, v]
    _rclone("config", "create", name, backend, *kv, "--obscure", timeout=600 if typ == "drive" else 60)
    try:
        _rclone("lsd", name + ":", timeout=90)
    except HTTPException as ex:
        _rclone("config", "delete", name, timeout=20)
        raise HTTPException(502, "Zugang zu %s fehlgeschlagen, Remote wieder entfernt: %s" % (typ, ex.detail))
    return {"ok": True, "remote": name}


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
def vorschau(quelle: str, pfad: str, mandant: str = MANDANT, seite: int = 0):
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
        raise HTTPException(400, "Register nur fuer lokale Ordner")
    voll = _sicher(q["pfad"], body.get("pfad", ""))
    con = _register_con(mandant)
    n = wolke = 0
    with bp.Protokoll(bp.pfad_fuer(mandant)) as prot:
        prot.schreiben("notiz", werkzeug="trove-plugin", mandant=mandant, meldung="Register-Lauf beginnt: " + voll)
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
        prot.schreiben("notiz", werkzeug="trove-plugin", mandant=mandant, meldung="Register-Lauf beendet: %d neue Dateien, %d nur in der Wolke (iCloud-Platzhalter, nicht gelesen)" % (n, wolke))
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
        raise HTTPException(400, "art muss kopieren, verschieben oder sync sein")
    if _kundenmodus():
        raise HTTPException(403, "Gesperrt: Trove liest deine Ablage nur. Kopieren, Verschieben und Sync in eine Quelle sind hier abgeschaltet.")
    _namen_pruefen(body.get("namen") or [])
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
                if n and os.path.lexists(os.path.join(von_ordner, n)) and not _unter_real(os.path.join(von_ordner, n), von_ordner):
                    raise HTTPException(400, "Pfad ausserhalb der Quelle (Verknuepfung): %s" % n)
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
    if q["art"] != "ordner":
        raise HTTPException(400, "F2-Lauf nur fuer lokale Ordner")
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
        raise HTTPException(502, "hermes-Programm nicht gefunden (nicht im PATH). Pfad per TROVE_HERMES setzen.")
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
                             "--workspace", "dir:" + (os.environ.get("TROVE_ARBEITSORDNER") or TROVE_REPO), "--max-retries", "1", "--max-runtime", "900", "--skill", "ponytail")
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
FITZ_SKRIPTE = ("lese_treppe.py", "stapel.py", "wissen.py", "pdf_seite.py")  # brauchen pypdfium2/pikepdf; die anderen laufen in jedem Python
OHNE_FITZ_MOEGLICH = ("wissen.py",)  # laeuft notfalls ohne pypdfium2: Text/Markdown/Word gelesen, PDFs mit klarer Meldung


def _werkzeug(skript, *args, timeout=1800, ok_rc=(0,)):
    """OCR/PDF laufen im System-Python (pypdfium2, pikepdf, rapidocr) als Unterprozess — der Hermes-Server hat ein eigenes Python ohne
    diese Pakete. ok_rc: Rueckgabewerte, bei denen die letzte Zeile die JSON-Zusammenfassung ist (reraise: 2 = Abbruch)."""
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
        if os.name == "nt":  # Enkel (qmd/node, brett.py) haengen sonst weiter: nur /T erreicht den ganzen Baum
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            p.kill()
        try:
            p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        raise HTTPException(504, skript + ": Zeit abgelaufen, Prozessbaum beendet")
    out, err = out or "", err or ""
    if p.returncode not in ok_rc:
        raise _fehler(500, "%s fehlgeschlagen (rc %d): %s" % (skript, p.returncode, (err or out)[-800:]))
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise _fehler(500, skript + ": keine JSON-Ausgabe: " + out[-400:] + (" | stderr: " + err[-400:] if err else ""))


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
    r"""F6: mehrseitiges PDF in Belege zerlegen (Stirling-PDF), Ergebnis unter <mandant>\stapel\<sha12>\, F8 'zerlegt' je Beleg."""
    mandant = body.get("mandant") or MANDANT
    voll = _quell_datei(mandant, body)
    r = _werkzeug("stapel.py", voll, "--mandant", mandant, "--json", ok_rc=(0, 3))
    if r.get("fehler"):
        raise HTTPException(503, r["fehler"])
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
    """DocuSeal gehoert nicht zum Kundenpaket: im Kundenmodus jede DocuSeal-Route 403 (wie /reraise); vor dem try der Route rufen."""
    if _kundenmodus():
        raise HTTPException(403, "Unterschriften (DocuSeal) sind im Kundenmodus ausgeschaltet")
    import docuseal  # nur urllib
    return docuseal


@router.get("/docuseal/vorlagen")
def docuseal_vorlagen():
    ds = _ds()
    try:
        return {"vorlagen": ds.vorlagen()}
    except Exception as ex:
        raise HTTPException(502, "DocuSeal: " + str(ex)[:300])


@router.post("/docuseal/senden")
def docuseal_senden(body: dict):
    """Vorgang aus Vorlage anlegen; 'quelle'+'pfad' = Original (Rueckverfolgung), 'an' = Unterzeichner, 'mail' = DocuSeal mailt selbst."""
    ds = _ds()
    mandant = body.get("mandant") or MANDANT
    beleg = _quell_datei(mandant, body) if body.get("quelle") and body.get("pfad") else None
    if not body.get("an") or not body.get("vorlage"):
        raise HTTPException(400, "vorlage und an (E-Mail) angeben")
    try:
        return {"ok": True, **ds.senden(mandant, int(body["vorlage"]), body["an"], body.get("name", ""), body.get("rolle"), beleg, bool(body.get("mail")), body.get("betreff"))}
    except Exception as ex:
        raise HTTPException(502, "DocuSeal: " + str(ex)[:300])


@router.post("/docuseal/abholen")
def docuseal_abholen(body: dict = None):
    body = body or {}
    ds = _ds()
    try:
        return {"ok": True, "vorgaenge": ds.abholen(body.get("mandant") or MANDANT, body.get("vorgang"))}
    except Exception as ex:
        raise HTTPException(502, "DocuSeal: " + str(ex)[:300])


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
        raise HTTPException(400, "max: ganze Zahl")
    if n < 1:
        raise HTTPException(400, "max: mindestens 1")  # 0 hiesse im Werkzeug "alle"
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
    """F16: Ordner einer Quelle lesen -> Markdown je Datei (mit Original verheiratet) -> QMD-Sammlung -> optional Beleg-Karten (brett.py).
    sichtbarkeit privat = eigene Nutzung, oeffentlich = Reraise-Tausch. Laeuft als Unterprozess im System-Python."""
    mandant = _mandant_pruefen(body.get("mandant") or MANDANT)
    q = _quelle(mandant, body.get("quelle", ""))
    if q["art"] != "ordner":
        raise HTTPException(400, "Ins Wissen nur aus lokalem Ordner (rclone-Quellen zuerst holen)")
    wurzel = _sicher(q["pfad"], body.get("pfad", ""))
    if not os.path.isdir(wurzel):
        raise HTTPException(404, "Ordner nicht gefunden")
    sicht = body.get("sichtbarkeit") or "privat"  # keine Pflichtfelder: Standard privat
    if sicht not in ("privat", "oeffentlich"):
        raise HTTPException(400, "sichtbarkeit: privat | oeffentlich")
    sammlung = _sammlung(mandant, body)
    if body.get("zugehoerigkeit") is not None and not isinstance(body.get("zugehoerigkeit"), str):
        raise HTTPException(400, "zugehoerigkeit: Text")
    zug = (body.get("zugehoerigkeit") or "").strip() or ZUG_OFFEN  # leer -> "offen"; die Belegbuchung fragt spaeter nach
    auswahl = body.get("dateien") or []
    if not isinstance(auswahl, list) or not all(isinstance(x, str) and x.strip() for x in auswahl) or len(auswahl) > 500:
        raise HTTPException(400, "dateien: Liste relativer Pfade (höchstens 500)")
    einzeln = []
    for rel in auswahl:
        voll = _sicher(q["pfad"], rel)  # realpath-Pruefung: nie ausserhalb der eingebundenen Quelle
        if not _unter_real(voll, wurzel) or not os.path.isfile(voll):
            raise HTTPException(400, "keine Datei im gewählten Ordner: %s" % rel)
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
        raise _fehler(409, r.get("abbruch") or "Lauf laeuft bereits")
    # Ordner fuer den Dauerlauf vormerken (dauerlauf.py fuehrt ihn bei geplantem Lauf automatisch nach); nie den eigenen Wissensspeicher
    if not r.get("eigene_ablage") and not einzeln:  # Einzelauswahl: Ordner NICHT dauerhaft nachfuehren
        konfig.ueberwacht_eintragen({"ordner": wurzel, "sammlung": sammlung, "sichtbarkeit": sicht, "zugehoerigkeit": zug,
                                 "traeger": body.get("traeger") or "", "karten": bool(body.get("karten"))})
    return {"ok": True, "nachgefuehrt": not r.get("eigene_ablage") and not einzeln, **{k: v for k, v in r.items() if k != "eintraege"}, "eintraege": r.get("eintraege", [])[:200]}


@router.post("/reraise")
def reraise(body: dict):
    """F16: oeffentliche Markdown-Zwillinge (Ordner oder Datei) als Karten zu reraise.ai; privat wird im Werkzeug verweigert.
    probe = Karten bauen und zeigen (je Karte sha256), nichts senden. Senden nur mit bestaetigt = [sha256 aus der Probe]; die Liste
    geht als Datei im Stand-Ordner an das Werkzeug (--nur-sha). Abbruch -> 200 mit "abbruch", laufender Lauf (lauf_laeuft) -> 409.
    bestaetigt-Eintraege "sha256:karte_hash" binden an den Karteninhalt der Probe. Schluessel nur als Datei auf dem Server."""
    if not _reraise_knopf():
        raise HTTPException(403, "Der Upload zu Reraise ist ausgeschaltet (Kundenmodus). Einschalten nur auf ausdruecklichen Wunsch: installieren.py --reraise-knopf an")
    mandant = _mandant_pruefen(body.get("mandant") or MANDANT)
    q = _quelle(mandant, body.get("quelle", ""))
    if q["art"] != "ordner":
        raise HTTPException(400, "Reraise nur aus lokalem Ordner (rclone-Quellen zuerst holen)")
    voll = _sicher(q["pfad"], body.get("pfad", ""))
    if not os.path.exists(voll):
        raise HTTPException(404, "nicht gefunden: " + body.get("pfad", ""))
    args = [voll, "--mandant", mandant, "--sammlung", _sammlung(mandant, body), "--json", *_max(body)]
    liste = None
    if body.get("probe"):
        args.append("--probe")
    else:
        b = body.get("bestaetigt")
        if not (isinstance(b, list) and 0 < len(b) <= 1000
                and all(isinstance(s, str) and re.fullmatch(r"[0-9a-f]{64}(:[0-9a-f]{64})?", s) for s in b)):
            raise HTTPException(400, "bestaetigt: Liste der sha256 (oder sha256:karte_hash) aus der Probe (ohne Liste wird nichts gesendet)")
        liste = os.path.join(TROVE_DATEN, mandant, "reraise-bestaetigt-%d-%d.txt" % (os.getpid(), time.time_ns()))
        os.makedirs(os.path.dirname(liste), exist_ok=True)
        with open(liste, "w", encoding="utf-8") as f:
            f.write("\n".join(b))
        args += ["--nur-sha", liste]
    try:
        r = _werkzeug("reraise_hochladen.py", *args, timeout=3600, ok_rc=(0, 2))
    finally:
        if liste:
            try:
                os.remove(liste)
            except OSError:
                pass
    if r.get("lauf_laeuft"):
        raise _fehler(409, r.get("abbruch") or "Lauf laeuft bereits")
    return {"ok": True, **{k: v for k, v in r.items() if k != "eintraege"}, "eintraege": r.get("eintraege", [])[:200]}
