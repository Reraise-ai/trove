"""Trove F16 -- folder into knowledge: read files -> machine-readable Markdown twin per file (bound to the original by sha256 +
receipt log) -> optional knowledge card in the wiki -> optional qmd search index.

Knowledge target (one plain setting, `wiki_wurzel` in trove.json / TROVE_WIKI): <wiki>/../raw/trove/<collection>/ holds the twins,
<wiki>/quellen/ the cards (status zu-pruefen), <wiki>/log.md one line per run. Default: <data root>/knowledge/wiki (created on demand).
The original file is never changed. A card is never written twice (file exists = card exists).
Receipt log (F8): per file a short lock for "gelesen" (before the twin is stored), a "notiz" per card; start/end/index as "notiz".
Duplicates (same sha256) are read once per run; further finds are reported as "dublette".
Visibility: privat = own use, oeffentlich = card reduced to the allow-list in oeffentlich.kartenfelder (nothing from the document text).
Run lock per client: gemeinsam.laufsperre. lese_treppe/verprobung (OCR packages) are only loaded when reading.
The qmd search tool is optional: without it the twins and cards are still written and the run reports that indexing was skipped.
"""
import argparse
import dataclasses
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import belegprotokoll as bp
import gemeinsam as gm
import oeffentlich as oe

def _qmd_exe():
    win = os.path.join(os.environ.get("LOCALAPPDATA", ""), r"hermes\node\qmd.cmd")
    mac = "/opt/homebrew/bin/qmd"
    return os.environ.get("TROVE_QMD") or (win if os.path.exists(win) else None) or shutil.which("qmd") or (mac if os.path.exists(mac) else "qmd")


QMD = _qmd_exe()
QMD_ENV = {"XDG_CACHE_HOME": os.environ.get("TROVE_QMD_CACHE", os.path.join(os.path.expanduser("~"), ".cache")),
           "XDG_CONFIG_HOME": os.environ.get("TROVE_QMD_CONFIG", os.path.join(os.path.expanduser("~"), ".config"))}
QMD_INDEX_YML = os.environ.get("TROVE_QMD_INDEX", os.path.join(QMD_ENV["XDG_CONFIG_HOME"], "qmd", "index.yml"))
ERZEUGT_VON = "trove/wissen.py"
MAX_TEXT_BYTES = 5 << 20
QMD_VERBOTEN = set('&|<>^%"!')  # qmd.cmd ist eine Batch-Datei (BatBadBut): solche Zeichen nie durchreichen


SAMMLUNG_STANDARD = "quellen"  # Ins Wissen ohne Sammlung: raw/trove/quellen, Karte wiki/quellen
ZUG_OFFEN = "offen"  # Ins Wissen ohne Zugehoerigkeit: als offen gekennzeichnet; die Belegbuchung fragt spaeter gezielt nach


def traeger_auto(zugehoerigkeit):
    """Traeger ohne Angabe: offen bei offener Zugehoerigkeit, Person bei 'privat' darin, sonst Firma."""
    z = (zugehoerigkeit or "").strip()
    if not z or z == ZUG_OFFEN:
        return ZUG_OFFEN
    return "Person" if "privat" in z.lower() else "Firma"


@dataclasses.dataclass
class Lauf:
    """Ein Lauf von ordner_ins_wissen; prueft Mandant/Sammlung beim Anlegen (die einzige Namenspruefung hier)."""
    mandant: str
    sammlung: str
    sichtbarkeit: str
    zugehoerigkeit: str
    traeger: str
    wurzel: str = ""
    karten: bool = False
    max_dateien: Optional[int] = None
    max_seiten: int = 30
    embed: bool = True
    nur_dateien: Optional[list] = None  # Auswahl im Tab: nur diese Dateien (unter wurzel), sonst ganzer Ordner
    index: Optional[dict] = None  # oeffentlich.zwillings_index, einmal je Lauf
    name: str = dataclasses.field(init=False)
    ordner: str = dataclasses.field(init=False)

    def __post_init__(self):
        gm.pruefe_namen(self.mandant, self.sammlung)
        self.name = "wissen-" + self.sammlung
        import wissenskarte as wk
        # only the text twin, directly in the knowledge target (raw/trove/<collection>), nothing stored twice
        self.ordner = wk.raw_ziel(self.sammlung)


def sichtbarkeit_regel(sichtbarkeit):
    """Die einzige Stelle, die Sichtbarkeit -> Kartenfelder abbildet."""
    regeln = {"privat": {"sichtbarkeit": "privat", "freigabe": "privat"},
              "oeffentlich": {"sichtbarkeit": "oeffentlich", "freigabe": "community"}}
    if sichtbarkeit not in regeln:
        raise ValueError("sichtbarkeit: privat | oeffentlich")
    return regeln[sichtbarkeit]


def _slug(s, n=60):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:n] or "datei"


def _norm(p):
    return os.path.normcase(os.path.abspath(p)).casefold()


def _unter(p, basis):
    n = _norm(p)
    return n == basis or n.startswith(basis.rstrip(os.sep) + os.sep)


def _prot(mandant):
    return gm.prot_mit_wiederholung(bp.pfad_fuer(mandant))


def _notiz(l, ereignis="notiz", anker=False, **felder):
    with _prot(l.mandant) as prot:
        prot.schreiben(ereignis, mandant=l.mandant, lauf=l.name, **felder)
        if anker:
            prot.anker_setzen(l.mandant, l.name)


def zwilling_lesen(md):
    with open(md, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    return gm.kopf_lesen(text), text


def _ist_zwilling(pfad):
    try:
        with open(pfad, encoding="utf-8", errors="replace") as fh:
            anfang = fh.read(4096)
    except OSError:
        return False
    return ('erzeugt_von: "%s"' % ERZEUGT_VON) in gm.kopf_grenzen(anfang + "\n---\n")[0]


def _verproben(felder):
    import verprobung as vp
    v = vp.verproben(felder)
    return ("sicher" if v["sicher"] else "bestanden" if v["bestanden"] else "prueflist"), v["gruende"]


class FitzFehlt(RuntimeError):
    """PDF/Bild ohne pypdfium2: diese Datei wird nicht gelesen; Text-, Markdown- und Word-Dateien laufen weiter."""


_FITZ = []


def fitz_da():
    if os.environ.get("TROVE_OHNE_PDF") == "1":
        return False
    if not _FITZ:
        try:
            import pypdfium2  # noqa: F401
            _FITZ.append(True)
        except ImportError:
            _FITZ.append(False)
    return _FITZ[0]


def docx_text(pfad):
    """Text einer Word-Datei (.docx) nur mit der Standardbibliothek: Absaetze aus word/document.xml."""
    import zipfile
    import xml.etree.ElementTree as ET
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(pfad) as z:
        info = z.getinfo("word/document.xml")
        if info.file_size > 8 * MAX_TEXT_BYTES:
            raise ValueError("Word file too large (document.xml %d bytes)" % info.file_size)
        wurzel = ET.fromstring(z.read(info))
    return "\n".join("".join(t.text or "" for t in p.iter(w + "t")) for p in wurzel.iter(w + "p"))[:MAX_TEXT_BYTES]


def _lesen(pfad, max_seiten, sha):
    import lese_treppe as lt
    klein = pfad.lower()
    if klein.endswith((".txt", ".md", ".docx")):
        if klein.endswith(".docx"):
            text, methode = docx_text(pfad), "docx"
        else:
            with open(pfad, "rb") as fh:
                text, methode = fh.read(MAX_TEXT_BYTES).decode("utf-8", errors="replace"), "text"
        return {"text": text, "methode": methode, "seiten": 1, "gelesene_seiten": 1, "dauer_s": 0.0,
                "felder": lt.felder_aus_text(text), "sha256": sha}
    if not fitz_da():
        raise FitzFehlt("PDF/image not read: package pypdfium2 is missing in Python %s (the health check names the command). "
                        "Text, markdown and Word files are read." % sys.executable)
    if klein.endswith(UMWANDELN):
        return _umgewandelt_lesen(pfad, max_seiten, sha)
    return lt.lesen(pfad, "rapidocr", False, max_seiten)


UMWANDELN = (".heic", ".numbers", ".pages", ".key")  # Mac-Formate: vor dem Lesen umgewandelt (_umgewandelt_lesen)
LESBAR = gm.ENDUNGEN + UMWANDELN
IWORK_VORSCHAU = ("quicklook/preview.pdf", "preview.pdf", "preview.jpg", "quicklook/thumbnail.jpg")  # bestes zuerst
MAX_VORSCHAU_BYTES = 64 << 20


def _umgewandelt_lesen(pfad, max_seiten, sha):
    """.heic -> JPEG mit dem macOS-Werkzeug sips; .numbers/.pages/.key (ZIP) -> eingebettete Vorschau. Danach der PDF-/Bildweg.
    shortcut: von iWork liest Trove nur die eingebettete Vorschau (neue Formate: Bild der ersten Seite), volle Texte erst mit PDF-Export."""
    import tempfile
    import zipfile
    import lese_treppe as lt
    typ = os.path.splitext(pfad)[1].lower()
    with tempfile.TemporaryDirectory(prefix="trove-umw-") as tmp:
        if typ == ".heic":
            ziel = os.path.join(tmp, "bild.jpg")
            try:
                r = subprocess.run(["sips", "-s", "format", "jpeg", pfad, "--out", ziel], capture_output=True, text=True, timeout=120)
            except FileNotFoundError:
                raise ValueError(".heic not read: conversion needs the macOS tool sips")
            if r.returncode or not os.path.isfile(ziel):
                raise ValueError(".heic not read: sips reports %s" % ((r.stderr or r.stdout).strip()[:200] or "error %d" % r.returncode))
            art = "heic->jpeg"
        else:
            if not zipfile.is_zipfile(pfad):
                raise ValueError("%s not read: not a single-file package (damaged or other format)" % typ)
            with zipfile.ZipFile(pfad) as z:
                namen = {n.lower(): n for n in z.namelist()}
                treffer = next((namen[k] for k in IWORK_VORSCHAU if k in namen), None)
                if not treffer:
                    raise ValueError("%s not read: no embedded preview in the document (export it as PDF from the app)" % typ)
                if z.getinfo(treffer).file_size > MAX_VORSCHAU_BYTES:
                    raise ValueError("%s not read: preview too large" % typ)
                ziel = os.path.join(tmp, "vorschau" + os.path.splitext(treffer)[1].lower())
                with open(ziel, "wb") as fh:
                    fh.write(z.read(treffer))
            art = "Vorschau " + treffer
        erg = lt.lesen(ziel, "rapidocr", False, max_seiten)
    erg.update(datei=pfad, sha256=sha, methode="%s (%s)" % (erg["methode"], art))
    return erg


LESEKOPIE = "This is a reading copy, not the file itself."
KEIN_TEXT = "(no text recognised)"
KEIN_TEXT_ALT = "(kein Text erkannt)"  # twins written by earlier versions


def original_zeile(pfad):
    """Erste Textzeile jedes Zwillings: wo das Original liegt (absolut). Der Agent soll diesen Pfad nennen, nicht den Zwilling."""
    return "Original is located at: %s" % pfad


def hinweis_kopf(pfad):
    return "%s -- %s" % (original_zeile(pfad), LESEKOPIE)


def lesbarer_text(md_text):
    """Text des Zwillings (Abschnitt '## Text') oder "", wenn nichts Lesbares darin steht (leer, Platzhalter, nur Zeichen ohne Buchstabe/Ziffer)."""
    text = md_text.split("\n## Text\n", 1)[1].strip() if "\n## Text\n" in md_text else ""
    return text if text not in (KEIN_TEXT, KEIN_TEXT_ALT) and re.search(r"\w", text) else ""


def inhaltszeile_mit_titel(md_text, n=200):
    """'Inhalt: <ganzer Kurztext>' oder 'Inhalt (Anfang): <~n Zeichen> …'; "" ohne lesbaren Text."""
    anfang = inhaltszeile(md_text, n) if lesbarer_text(md_text) else ""
    if not anfang:
        return ""
    return ("Content (start): " if anfang.endswith(" …") else "Content: ") + anfang


def inhaltszeile(md_text, n=200):
    """Kurzer, bereinigter Inhaltsanfang (Abschnitt '## Text') fuer die Wiki-Karte, damit die Suche im Wiki die Karte findet."""
    text = md_text.split("\n## Text\n", 1)[1] if "\n## Text\n" in md_text else ""
    text = re.sub(r"[`*_>|\[\]#]+", " ", text)
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    kurz = text[:n]
    return (kurz.rsplit(" ", 1)[0] if " " in kurz[n // 2:] else kurz) + " …"


def markdown_erzeugen(l, pfad):
    """Eine Datei -> Markdown-Zwilling. Idempotent ueber sha256 (sha16-Praefix). Protokoll nur kurz fuer 'gelesen'."""
    regel = sichtbarkeit_regel(l.sichtbarkeit)
    os.makedirs(l.ordner, exist_ok=True)
    sha = bp.datei_sha(pfad)
    if l.index is None:
        vorhanden = sorted(glob.glob(os.path.join(glob.escape(l.ordner), sha[:16] + "_*.md")))
    else:
        vorhanden = [md for md in l.index.get(sha[:16], ()) if os.path.dirname(md) == l.ordner]
    if vorhanden:
        kopf, text = zwilling_lesen(vorhanden[0])
        if kopf.get("pruefsumme") != sha:
            raise ValueError("Twin %s does not belong to sha256 %s (head: %s)" % (vorhanden[0], sha, kopf.get("pruefsumme")))
        if kopf.get("sichtbarkeit") != regel["sichtbarkeit"]:
            raise ValueError("Visibility differs (existing: %s) in %s" % (kopf.get("sichtbarkeit"), vorhanden[0]))
        roh, ende = gm.kopf_grenzen(text)
        if any(c in roh for c in "\u2028\u2029\u0085"):
            gm.schreibe_atomar(vorhanden[0], "---\n" + "\n".join("%s: %s" % (k, gm.yaml_wert(w)) for k, w in kopf.items()) + text[ende:])
            _notiz(l, werkzeug="wissen.py", quelle=pfad, ziel=vorhanden[0], sha256=sha,
                   meldung="Kopf bereinigt (U+2028/U+2029/U+0085 als Escape, Kopf ASCII)", md_sha256=bp.datei_sha(vorhanden[0]))
        return {"md": vorhanden[0], "sha256": sha, "zustand": "schon_da"}
    md = os.path.join(l.ordner, "%s_%s.md" % (sha[:16], _slug(os.path.splitext(os.path.basename(pfad))[0])))
    erg = _lesen(pfad, l.max_seiten, sha)
    f = json.loads(json.dumps(erg["felder"], default=str))
    verprobung, gruende = _verproben(erg["felder"])
    kopf = {"hinweis": hinweis_kopf(pfad), "titel": os.path.basename(pfad), "quelle": pfad, "quelle_datum": f.get("datum"), "pruefsumme": sha,
            "erzeugt": bp.jetzt(), "erzeugt_von": ERZEUGT_VON, "mandant": l.mandant, "sammlung": l.sammlung,
            "lesemethode": erg["methode"], "seiten": erg["seiten"], "gelesene_seiten": erg.get("gelesene_seiten"),
            "sichtbarkeit": regel["sichtbarkeit"], "freigabe": regel["freigabe"], "zugehoerigkeit": l.zugehoerigkeit,
            "traeger": l.traeger, "klasse": "B", "verprobung": verprobung,
            "belegdatum": f.get("datum"), "brutto": f.get("brutto"), "netto": f.get("netto"), "steuer": f.get("steuer"),
            "waehrung": f.get("waehrung"), "rechnungsnummer": f.get("rechnungsnummer"),
            "aussteller_kennung": f.get("cuit") or f.get("ust_id"), "iban": f.get("iban")}
    gruende = (" (" + "; ".join(gruende) + ")") if gruende else ""
    zeilen = ["---"] + ["%s: %s" % (k, gm.yaml_wert(w)) for k, w in kopf.items()] + ["---", "", original_zeile(pfad) + "  ", LESEKOPIE, "",
              "# " + os.path.basename(pfad), "",
              "Original: `%s`  " % pfad, "Checksum (sha256): `%s`  " % sha,
              "Read: %s, %s pages, verification %s%s" % (erg["methode"], erg["seiten"], verprobung, gruende), "",
              "## Felder", ""]
    zeilen += ["- %s: %s" % (k, w) for k, w in f.items() if w not in (None, "")]
    zeilen += ["", "## Text", "", (erg.get("text") or "").strip() or KEIN_TEXT, ""]
    inhalt = "\n".join(zeilen)
    meldung = "Markdown twin for the knowledge base (%s, %s pages, verification %s, visibility %s)"
    with _prot(l.mandant) as prot:
        prot.schreiben("gelesen", quelle=pfad, ziel=md, sha256=sha, bytes_=os.path.getsize(pfad), werkzeug="wissen.py",
                       mandant=l.mandant, lauf=l.name, meldung=meldung % (erg["methode"], erg["seiten"], verprobung, l.sichtbarkeit),
                       methode=erg["methode"], felder=f, sichtbarkeit=l.sichtbarkeit, sammlung=l.sammlung,
                       md_sha256=hashlib.sha256(inhalt.encode("utf-8", "replace")).hexdigest())
        gm.schreibe_atomar(md, inhalt)  # gleiche Bytes wie md_sha256 (utf-8, errors replace, \n)
    oe.index_eintragen(l.index, md)
    return {"md": md, "sha256": sha, "zustand": "neu"}


def _qmd_pruefen(ordner):
    if QMD_VERBOTEN & set(ordner):
        raise ValueError("Folder path contains characters forbidden for qmd.cmd (&|<>^%%\"!): %r" % ordner)


def _qmd_da():
    """Is the optional qmd tool available?"""
    return bool(shutil.which(QMD) or os.path.isfile(QMD))


def _qmd(*args, timeout=300):
    p = subprocess.Popen([QMD, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace", env={**os.environ, **QMD_ENV, "PYTHONUTF8": "1",
                                               "PATH": os.environ.get("PATH", "") + os.pathsep + "/opt/homebrew/bin" + os.pathsep + "/usr/local/bin"})
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":  # qmd.cmd -> cmd.exe -> node: nur /T erreicht den Enkel
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        else:
            p.kill()
        try:
            p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        raise RuntimeError("qmd %s: timed out (%d s), process tree terminated" % (args[0], timeout))
    return p.returncode, (out or "").strip()




def qmd_sammlung(l):
    """No collection of its own: the twin lives under <wiki>/../raw/trove/ and is found through the existing 'raw' collection;
    here only the index and embeddings of the default collections are refreshed (otherwise there would be double hits)."""
    import wissenskarte as wk
    erg = {"sammlung": "raw", "fehler": 0}
    if not _qmd_da():
        erg.update(uebersprungen="qmd (optional search index) is not installed - twins and cards were written, indexing skipped", dateien_im_index=0)
        return erg
    _qmd_pruefen(wk.raw_ziel(l.sammlung))
    rc, out = _qmd("update", timeout=900)
    erg["update"] = (rc, out[-400:])
    erg["fehler"] += rc != 0
    if l.embed:
        rc, out = _qmd("embed", "--timeout", "20", timeout=1800)
        erg["embed"] = (rc, out[-400:])
        erg["fehler"] += rc != 0
    ziel = wk.raw_ziel(l.sammlung)
    erg["dateien_im_index"] = len([f for f in os.listdir(ziel) if f.endswith(".md")]) if os.path.isdir(ziel) else 0
    return erg








def kartentext(kopf, md_text, md, l):
    """Private Karte: voller Zwilling (gekuerzt) mit Verweis auf Zwilling und Sammlung."""
    _, ende = gm.kopf_grenzen(md_text)
    body = md_text[ende + 5:] if ende >= 0 else md_text
    kurz = body[:6000] + ("\n\n(shortened; full text in the markdown twin)\n" if len(body) > 6000 else "")
    kopfzeile = "**Markdown twin:** `%s`  \n**Trove collection:** `trove-%s-%s` (qmd, `-c`)  \n**Verification:** %s\n\n"
    return kopfzeile % (md, l.mandant, l.sammlung, kopf.get("verprobung")) + kurz


def privat_titel(kopf):
    """Titel privater Karten, nur aus dem Zwilling-Kopf: [Belegdatum ]titel[ — Brutto Waehrung]."""
    if not kopf.get("titel"):
        raise ValueError("Twin has no title in its head")
    t = ("%s " % kopf["belegdatum"] if kopf.get("belegdatum") else "") + str(kopf["titel"])
    if kopf.get("brutto") is not None:
        t += " — %s %s" % (kopf["brutto"], kopf.get("waehrung") or "")
    return t.strip()[:120]


def _karte_im_kopf(md, kid):
    with open(md, encoding="utf-8") as fh:
        text = fh.read()
    _, ende = gm.kopf_grenzen(text)
    if ende < 0:
        raise ValueError("Twin has no head data: " + md)
    gm.schreibe_atomar(md, text[:ende] + "\nkarte: " + gm.yaml_wert(kid) + text[ende:])


def quelle_umziehen(l, md, neu):
    """Datei umbenannt/verschoben (gleiche sha256, alter Pfad nicht mehr da): Zwilling (Kopf quelle/titel, Zeile "Original:") und
    Wiki-Karte ("Liegt bei dir unter ...") auf den neuen Pfad umschreiben. Liefert den alten Pfad oder None (nichts zu tun)."""
    import wissenskarte as wk
    kopf, text = zwilling_lesen(md)
    alt = str(kopf.get("quelle") or "")
    if not alt or alt == neu or os.path.lexists(alt):  # gleicher Ort oder Dublette (alter Fundort lebt): nichts umschreiben
        return None
    roh, ende = gm.kopf_grenzen(text)
    zeilen = []
    for z in roh.split("\n"):
        k = z.partition(": ")[0]
        zeilen.append("quelle: " + gm.yaml_wert(neu) if k == "quelle" else "hinweis: " + gm.yaml_wert(hinweis_kopf(neu))
                      if k == "hinweis" else "titel: " + gm.yaml_wert(os.path.basename(neu))
                      if k == "titel" and kopf.get("titel") == os.path.basename(alt) else z)
    rumpf = text[ende:].replace("Original: `%s`" % alt, "Original: `%s`" % neu, 1)
    rumpf = rumpf.replace("\n" + original_zeile(alt) + "  \n", "\n" + original_zeile(neu) + "  \n", 1)
    gm.schreibe_atomar(md, "---\n" + "\n".join(zeilen) + rumpf)
    karte = str(kopf.get("karte") or "")
    if karte.startswith("wiki:"):
        cp = os.path.join(wk.wiki_wurzel(), *karte[5:].split("/")) + ".md"
        if os.path.exists(cp):
            with open(cp, encoding="utf-8") as fh:
                ct = fh.read()
            nt = ct.replace("Located at `%s`." % alt, "Located at `%s`." % neu)
            nt = nt.replace("\nsource: %s\n" % " ".join(alt.split()), "\nsource: %s\n" % " ".join(neu.split()), 1)  # Urheber-Kopf (19.10.)
            if nt != ct:
                gm.schreibe_atomar(cp, nt)
                wk.GEAENDERT.append(cp)
    _notiz(l, werkzeug="wissen.py", quelle=neu, ziel=md, sha256=kopf.get("pruefsumme"),
           meldung="Original renamed/moved: %s -> %s (twin and card updated)" % (alt, neu))
    return alt








def karte_erzeugen(l, md_erg, min_zeichen=1):
    """Wissenskarte aus dem Zwilling. Wahrheit: Kopfzeile 'karte:'. Zugehoerigkeit/Traeger aus dem Kopf.
    Oeffentlich: nur Positivliste (oeffentlich.kartenfelder); verweigert, wenn dieselbe Datei irgendwo privat ist.
    ProtokollGesperrt/ProtokollDefekt gehen unveraendert an den Aufrufer."""
    regel = sichtbarkeit_regel(l.sichtbarkeit)
    md, sha = md_erg["md"], md_erg["sha256"]
    kopf, md_text = zwilling_lesen(md)
    if kopf.get("karte"):
        return {"karte": kopf["karte"], "grund": "card already existed"}
    zugehoerigkeit, traeger = kopf.get("zugehoerigkeit") or ZUG_OFFEN, kopf.get("traeger") or ZUG_OFFEN
    if regel["sichtbarkeit"] == "oeffentlich":
        privat = oe.privat_irgendwo(os.path.dirname(l.ordner), sha, l.index)
        if privat:
            return {"karte": None, "grund": "private in collection %s" % ", ".join(privat)}
        k = oe.kartenfelder(sha, kopf)
        titel, quelle, inhalt, datum = k["titel"], k["quelle"], k["body"], k["datum"]
    else:
        text = lesbarer_text(md_text)  # jede Datei mit lesbarem Text bekommt eine Karte, auch eine Ein-Zeilen-Notiz
        if not text or len(text) < min_zeichen:
            return {"karte": None, "grund": "no readable text" if not text else "too little text (%d characters)" % len(text)}
        titel, quelle, inhalt = privat_titel(kopf), str(kopf.get("quelle") or ""), kartentext(kopf, md_text, md, l)
        datum = oe.iso_datum(kopf.get("belegdatum"))
    import wissenskarte as wk
    if regel["sichtbarkeit"] == "privat":  # Karte verweist nur auf die Quelle (raw/trove/...), der Volltext steht dort einmal -> keine Doppeltreffer
        felder = md_text.split("\n## Felder", 1)[1].split("\n## Text", 1)[0].strip() if "\n## Felder" in md_text and "\n## Text" in md_text else ""
        anfang = inhaltszeile_mit_titel(md_text)  # kurze Datei: voller Text ("Inhalt: ..."), sonst Anfang
        inhalt = "Located at `%s`.\n\n%sRead as a document twin (verification: %s). Full text in the source.%s" % (
            kopf.get("quelle"), (anfang + "\n\n") if anfang else "", kopf.get("verprobung"),
            ("\n\n" + felder) if felder else "")
    kid, neu = wk.karte_schreiben(l.sammlung, md, sha, titel, inhalt, kopf.get("quelle"), zugehoerigkeit)
    _karte_im_kopf(md, kid)
    _notiz(l, quelle=md, ziel=kid, sha256=sha, werkzeug="wissen.py/wiki", karte=kid, sichtbarkeit=l.sichtbarkeit, md_sha256=bp.datei_sha(md),
           meldung="Knowledge card %s (%s, %s)" % (kid, "new" if neu else "existed, head line added", titel[:80]))
    return {"karte": kid, "grund": None, "karte_neu": neu}


FREIGABE = "macOS permission needed (System Settings > Privacy & Security > Full Disk Access)"


def unlesbar_meldung(pfad):
    return "Folder not readable: %s - %s" % (pfad, FREIGABE)


def unter_einem(p, ordner):
    """Liegt p in (oder ist p) einem der Ordner?"""
    return any(_unter(p, _norm(o)) for o in ordner)


def _echt(p):
    return _norm(os.path.realpath(os.path.expanduser(p)))


def eigene_ablagen(mandant):
    """Ordner, die Trove selbst beschreibt und darum nie einliest (sonst liest Trove seine eigenen Karten, Lauf fuer Lauf mehr):
    Knowledge folder (wiki root and its parent: raw/trove, wiki/quellen, wiki/log.md), Datenwurzel.
    Liefert [(normierter echter Pfad, Anzeigepfad, art)]; art "wissen" = nie einlesen, "daten" = Datenwurzel."""
    import wissenskarte as wk
    wiki = wk.wiki_wurzel(anlegen=False).rstrip("/\\") or wk.wiki_wurzel(anlegen=False)
    zonen = [(wiki, "wissen"), (os.path.dirname(wiki), "wissen")]
    zonen += [(bp.WURZEL, "daten"), (os.path.join(bp.WURZEL, mandant, "wissen"), "wissen")]
    out, gesehen = [], set()
    for p, art in zonen:
        n = _echt(p)
        if n not in gesehen and os.path.dirname(n) != n:  # nie das Laufwerk / die Wurzel selbst
            gesehen.add(n)
            out.append((n, p, art))
    return out


def nicht_gelesen_hinweis(liste):
    """Sichtbarer Satz fuer Dateien, deren Typ Trove nicht liest (None, wenn keine)."""
    if not liste:
        return None
    typen = {}
    for p in liste:
        t = (os.path.splitext(p)[1].lower() or "(no extension)") + (" (package folder)" if os.path.isdir(p) else "")
        typen[t] = typen.get(t, 0) + 1
    return "not read: %d %s, types %s" % (len(liste), "file" if len(liste) == 1 else "files",
                                               ", ".join("%s (%d)" % (t, n) for t, n in sorted(typen.items())))


def dateien_sammeln(mandant, wurzel, unlesbar=None, hinweise=None, nicht_gelesen=None):
    """Dateien unter wurzel (sortiert). unlesbar: Liste, in die jeder nicht lesbare Ordner kommt (macOS-Datenschutz, Rechte) —
    der Aufrufer darf darunter nichts als verschwunden werten. hinweise: Liste fuer sichtbare Warnungen.
    nicht_gelesen: Liste fuer Dateien, deren Typ Trove nicht liest (auch nur in der Wolke; ohne versteckte und ~$-Dateien).
    Nie eingelesen: versteckte Ordner und Trove-eigene Ablagen (eigene_ablagen). Ist der gewaehlte Ordner selbst der Wissensspeicher
    (oder liegt darin), wird nichts eingelesen. Liegt er in der Datenwurzel (z. B. Mail-Archiv), bleibt nur deren wissen/ aussen vor."""
    fehler = unlesbar if unlesbar is not None else []
    warn = hinweise if hinweise is not None else []
    ng = nicht_gelesen if nicht_gelesen is not None else []
    dateien, zwillinge, wolke = [], 0, []
    w_echt = _echt(wurzel)
    sperren = []
    for n, anzeige, art in eigene_ablagen(mandant):
        if _unter(w_echt, n) and art == "wissen":
            warn.append("The chosen folder is your knowledge folder (%s) - Trove does not read it in, otherwise it would read its own cards." % anzeige)
            return dateien, zwillinge, wolke
        if _unter(w_echt, n):  # in der Datenwurzel gewaehlt: ausdruecklich, nur die Ablagen darin bleiben gesperrt
            continue
        sperren.append((n, anzeige))
    for r, d, f in os.walk(wurzel, onerror=lambda ex: fehler.append(ex.filename or str(ex))):
        bleiben = []
        for x in sorted(d):
            if x.startswith("."):
                continue
            if x.lower().endswith(UMWANDELN):  # Paket-Ordner von Pages/Numbers/Keynote: nicht hineinsteigen, nicht gelesen
                ng.append(os.path.join(r, x))
                continue
            n = _echt(os.path.join(r, x))
            treffer = next((a for s, a in sperren if _unter(n, s)), None)
            if treffer:
                warn.append("Skipped: %s is Trove's own storage (%s) and is never read in." % (os.path.join(r, x), treffer))
                continue
            bleiben.append(x)
        d[:] = bleiben
        for fn in sorted(f):
            p = os.path.join(r, fn)
            name = os.path.basename(gm.wolke_original(p))
            if not name.lower().endswith(LESBAR) or name.startswith("~$"):  # Typ zuerst: Wolke zaehlt nur lesbare Dateien
                if not name.startswith((".", "~$")):
                    ng.append(gm.wolke_original(p))
                continue
            if gm.nur_in_wolke(p, fn):  # nur in der Wolke (.icloud oder dataless): zaehlen und melden, nicht lesen, kein Download
                wolke.append(p)
                continue
            if fn.lower().endswith(".md") and _ist_zwilling(p):
                zwillinge += 1
                continue
            dateien.append(p)
    return dateien, zwillinge, wolke


def dateien_auswahl(mandant, wurzel, pfade, hinweise, nicht_gelesen):
    """Wie dateien_sammeln, aber nur fuer ausdruecklich gewaehlte Dateien: jede per realpath unter wurzel, gleiche Regeln
    (Typ lesbar, nicht nur in der Wolke, kein Zwilling, nie aus Trove-eigenen Ablagen). Das Original wird nur gelesen."""
    dateien, zwillinge, wolke = [], 0, []
    w_echt = _echt(wurzel)
    sperren = [(n, a) for n, a, _ in eigene_ablagen(mandant)]
    for p in pfade:
        p = os.path.abspath(p)
        if not _unter(_echt(p), w_echt):
            raise ValueError("File is not inside the chosen folder: %s" % p)
        if not os.path.isfile(p):
            hinweise.append("Not found or no file content: %s" % p)
            continue
        treffer = next((a for s, a in sperren if _unter(_echt(p), s)), None)
        if treffer:
            hinweise.append("Skipped: %s is Trove's own storage (%s) and is never read in." % (p, treffer))
            continue
        fn = os.path.basename(p)
        name = os.path.basename(gm.wolke_original(p))
        if not name.lower().endswith(LESBAR) or name.startswith("~$"):
            nicht_gelesen.append(gm.wolke_original(p))
            continue
        if gm.nur_in_wolke(p, fn):
            wolke.append(p)
            continue
        if fn.lower().endswith(".md") and _ist_zwilling(p):
            zwillinge += 1
            continue
        if p not in dateien:
            dateien.append(p)
    return dateien, zwillinge, wolke


def ordner_ins_wissen(mandant, wurzel, sammlung, sichtbarkeit, zugehoerigkeit, traeger=None, karten=False, max_dateien=None,
                      max_seiten=30, embed=True, nur_dateien=None):
    regel = sichtbarkeit_regel(sichtbarkeit)
    zugehoerigkeit = (zugehoerigkeit or "").strip() or ZUG_OFFEN  # keine Pflicht
    sammlung = (sammlung or "").strip() or SAMMLUNG_STANDARD
    l = Lauf(mandant=mandant, sammlung=sammlung, sichtbarkeit=sichtbarkeit, zugehoerigkeit=zugehoerigkeit, wurzel=wurzel,
             traeger=traeger or traeger_auto(zugehoerigkeit), karten=karten,
             max_dateien=max_dateien, max_seiten=max_seiten, embed=embed, nur_dateien=list(nur_dateien) if nur_dateien else None)
    if regel["sichtbarkeit"] == "oeffentlich" and zugehoerigkeit == ZUG_OFFEN:
        raise ValueError("visibility oeffentlich (public) needs an affiliation (not offen) - aborted")
    if regel["sichtbarkeit"] == "oeffentlich" and "privat" in zugehoerigkeit.lower():
        raise ValueError("oeffentlich (public) with a private affiliation (%s) - aborted" % zugehoerigkeit)
    if not os.path.isdir(wurzel):
        raise ValueError("not a folder: %s" % wurzel)
    os.makedirs(l.ordner, exist_ok=True)
    with gm.laufsperre(l.mandant):
        for muster in ("*.md.teil", "*.md.karte.tmp"):
            for rest in glob.glob(os.path.join(glob.escape(l.ordner), muster)):
                os.remove(rest)
        l.index = oe.zwillings_index(os.path.dirname(l.ordner))  # twins live in raw/trove
        return _lauf(l)




def schon_oeffentlich(l, sha, md):
    """Private run: another twin of the same file is public and already has a card -> message text, else None."""
    funde = []
    for s, anderer, kopf, _ in oe.zwillinge(os.path.dirname(l.ordner), sha, l.index):
        if anderer == md or kopf.get("pruefsumme") != sha or kopf.get("sichtbarkeit") != "oeffentlich":
            continue
        if kopf.get("karte"):
            funde.append("collection %s (card %s)" % (s, kopf["karte"]))
    return ("already public: " + "; ".join(funde)) if funde else None


def wolke_hinweis(n):
    """Meldung fuer den Tab nach 'Ins Wissen': Dateien, die macOS nur in der Wolke haelt (dataless/.icloud), wurden nicht gelesen."""
    if not n:
        return None
    return "%d %s only in the cloud - in Finder choose Download Now, then run Into knowledge again." % (n, "file" if n == 1 else "files")


def _lauf(l):
    unlesbar, hinweise, nicht_gelesen = [], [], []
    if l.nur_dateien:
        dateien, zwillinge, wolke = dateien_auswahl(l.mandant, l.wurzel, l.nur_dateien, hinweise, nicht_gelesen)
    else:
        dateien, zwillinge, wolke = dateien_sammeln(l.mandant, l.wurzel, unlesbar, hinweise, nicht_gelesen)
    ng_hinweis = nicht_gelesen_hinweis(nicht_gelesen)
    if l.max_dateien:
        dateien = dateien[:l.max_dateien]
    z = {"mandant": l.mandant, "sammlung": l.sammlung, "wurzel": l.wurzel, "sichtbarkeit": l.sichtbarkeit, "traeger": l.traeger,
         "dateien": len(dateien), "nur_in_wolke": len(wolke), "wolke_hinweis": wolke_hinweis(len(wolke)), "zwillinge_uebersprungen": zwillinge, "neu": 0, "schon_da": 0, "karten": 0,
         "schon_oeffentlich": 0, "fehler_datei": 0, "fehler_karte": 0,
         "fehler_qmd": 0, "dubletten": 0, "fehler": 0, "eintraege": [], "unlesbar": unlesbar,
         "hinweise": [unlesbar_meldung(u) for u in unlesbar] + hinweise + ([ng_hinweis] if ng_hinweis else []),
         "eigene_ablage": bool(hinweise) and not dateien and not l.nur_dateien, "auswahl": len(l.nur_dateien or []), "nicht_gelesen": nicht_gelesen[:200], "nicht_gelesen_anzahl": len(nicht_gelesen)}
    import wissenskarte as wk
    wk.GEAENDERT.clear()
    erste = {}  # erste: sha256 -> erste Fundstelle in diesem Lauf
    _notiz(l, werkzeug="wissen.py", meldung="%s into knowledge starts: %s -> collection %s (%d files, visibility %s, %s"
           ")" % ("Selection (%d chosen)" % len(l.nur_dateien) if l.nur_dateien else "Folder", l.wurzel, l.sammlung,
                                 len(dateien), l.sichtbarkeit, l.zugehoerigkeit))
    if ng_hinweis:
        _notiz(l, werkzeug="wissen.py", meldung=ng_hinweis, nicht_gelesen=nicht_gelesen[:200])
    for p in dateien:
        e = {"datei": p, "md": None, "sha256": None, "zustand": "fehler", "karte": None, "grund": None, "fehler": None}
        try:
            sha = bp.datei_sha(p)
            if sha in erste:
                e.update(sha256=sha, zustand="dublette", grund="gleicher Inhalt wie " + erste[sha])
                z["dubletten"] += 1
                z["eintraege"].append(e)
                continue
            erste[sha] = p
            e.update(markdown_erzeugen(l, p))
            if e["zustand"] == "schon_da":
                e["umbenannt_von"] = quelle_umziehen(l, e["md"], p)
        except (bp.ProtokollGesperrt, bp.ProtokollDefekt):
            raise
        except Exception as ex:
            z["fehler_datei"] += 1
            e["fehler"] = str(ex)[:200]
            if isinstance(ex, FitzFehlt):
                z["ohne_pymupdf"] = z.get("ohne_pymupdf", 0) + 1
            _notiz(l, "fehler", quelle=p, werkzeug="wissen.py", meldung="Reading/markdown: " + str(ex)[:300])
            z["eintraege"].append(e)
            continue
        z[e["zustand"]] += 1
        if l.karten:
            try:
                e.update(karte_erzeugen(l, e))
                z["karten"] += bool(e.get("karte_neu"))
            except (bp.ProtokollGesperrt, bp.ProtokollDefekt):
                raise
            except Exception as ex:
                z["fehler_karte"] += 1
                e["fehler"] = "Card: " + str(ex)[:200]
                _notiz(l, "fehler", quelle=p, werkzeug="wissen.py/wiki", meldung="Card: " + str(ex)[:300])
        if l.sichtbarkeit == "privat":
            grund = schon_oeffentlich(l, e["sha256"], e["md"])
            if grund:
                e.update(zustand="schon_oeffentlich", grund=grund + ("; " + e["grund"] if e.get("grund") else ""))
                z["schon_oeffentlich"] += 1
                z["fehler_datei"] += 1
                _notiz(l, "fehler", quelle=p, sha256=e["sha256"], werkzeug="wissen.py", meldung="Read in privately, " + grund)
        z["eintraege"].append(e)
    _notiz(l, anker=True, werkzeug="wissen.py", meldung="Files done, qmd next: %d new, %d already there, %d cards"
           % (z["neu"], z["schon_da"], z["karten"]))
    try:
        z["qmd"] = qmd_sammlung(l)
        z["fehler_qmd"] += z["qmd"]["fehler"]
    except Exception as ex:
        z["qmd"] = {"fehler": 1, "meldung": str(ex)[:300]}
        z["fehler_qmd"] += 1
    z["fehler"] = z["fehler_datei"] + z["fehler_karte"] + z["fehler_qmd"] + len(unlesbar)
    if z.get("ohne_pymupdf"):
        z["hinweise"].append("%d PDF/image file(s) not read: package pypdfium2 is missing (Python %s). Text, markdown and Word files "
                             "were read; install pypdfium2 and run Into knowledge again." % (z["ohne_pymupdf"], sys.executable))
    zusammen = json.dumps({k: v for k, v in z.items() if k not in ("eintraege", "qmd")}, ensure_ascii=False)
    with _prot(l.mandant) as prot:
        prot.schreiben("notiz" if not z["fehler_qmd"] else "fehler", werkzeug="wissen.py/qmd", mandant=l.mandant, lauf=l.name,
                       ziel=l.ordner, meldung="qmd collection %s: %s files in the index" % (z["qmd"].get("sammlung"),
                       z["qmd"].get("dateien_im_index")), qmd=json.loads(json.dumps(z["qmd"], default=str)))
        prot.schreiben("notiz", werkzeug="wissen.py", mandant=l.mandant, lauf=l.name, meldung="Folder into knowledge finished: " + zusammen)
        prot.anker_setzen(l.mandant, l.name)
    try:
        z["log"] = wk.log_lauf()  # one line per run in wiki/log.md
    except Exception as ex:
        z["log_fehler"] = str(ex)[:200]
    return z


FACTURA_B = """FACTURA B  N° 0003-00012345
Fecha 01/03/2026
Ejemplo SA  CUIT 30-11111111-1  Ingresos Brutos 901-000000-0
Cliente: Juan Perez, Av. Rivadavia 1234, Piso 3, CABA
CUIT 20-12345678-9  DNI N.º 12.345.678  Tel 11 4567-8901
Alias juan.perez.mp  CBU 0000000000000000000000  juan@example.com
Detalle: servicio de consultoria mensual segun contrato
Subtotal 1.000,00  IVA 21% 210,00  Total $ 1.210,00
"""
RECHNUNG_DE = """Muster GmbH, Musterstr. 1, 44787 Bochum
Rechnungsnummer: RE-2026-0042
Datum 2026-03-05
Tel. 0234 1234567  Fax 0234 1234568  Mobil 0171 7654321
Steuernummer 350/123/45678  Steuer-ID 12 345 678 901  USt-IdNr. DE298765430
Kto-Nr. 1234567890  BLZ 43050001  IBAN DE89 3704 0044 0532 0130 00
Leistung: Beratung Maerz 2026 laut Vereinbarung vom Februar
Nettobetrag 100,00 EUR  MwSt 19% 19,00 EUR  Gesamtbetrag 119,00 EUR
"""
RECIBO_PERSONA = """Factura Nro. 12345678
Fecha 02/03/2026
Maria Lopez  CUIT 27-23456789-4  Domicilio Calle Falsa 123
Detalle: clases particulares de piano, cuatro encuentros en marzo
Lugar: domicilio de la alumna, horario de tarde, material incluido
Total $ 500,00
"""
GEHEIM = ("Juan", "Perez", "Rivadavia", "Piso", "12345678", "12.345.678", "4567", "juan.perez", "0000000000", "example", "@",
          "Muster", "Bochum", "0234", "1234567", "7654321", "350/123", "12 345 678 901", "1234567890", "43050001", "3704",
          "Maria", "Lopez", "Falsa", "27-23456789-4", "20-12345678-9", "901-000000-0", "consultoria", "Beratung", "piano",
          "0003-00012345", "30-11111111-1", "RE-2026-0042", "DE298765430", "factura", "rechnung", "recibo", ".txt", "eingang")






def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Trove: folder into knowledge (markdown twins, qmd index, cards)")
    ap.add_argument("ordner", nargs="?")
    ap.add_argument("--mandant", default=bp.konfig.mandant())
    ap.add_argument("--sammlung", default=SAMMLUNG_STANDARD, help="collection name (a-z 0-9 - _), default quellen")
    ap.add_argument("--sichtbarkeit", default="privat", help="privat = own use, oeffentlich = card limited to the public allow-list")
    ap.add_argument("--zugehoerigkeit", help="e.g. 'My Company Ltd' or 'Mine private'")
    ap.add_argument("--traeger", choices=["Person", "Firma", "Projekt", "Kunde", "Behoerde"],
                    help="ohne Angabe: Person, wenn 'privat' in der Zugehoerigkeit steht, sonst Firma")
    ap.add_argument("--karten", action="store_true", help="write a knowledge card per file into the wiki")
    ap.add_argument("--max", type=int, help="at most this many files (trial)")
    ap.add_argument("--datei", action="append", default=[], help="only these file(s) under the folder (repeatable); default: the whole folder")
    ap.add_argument("--max-seiten", type=int, default=30)
    ap.add_argument("--ohne-embed", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selbsttest", action="store_true")
    a = ap.parse_args()
    if a.selbsttest:
        gm.selbsttest()
        oe.selbsttest()
        print("selbsttest ok")
        return 0
    if not a.ordner:
        ap.error("give a folder (--sammlung, --sichtbarkeit, --zugehoerigkeit optional)")
    if a.max is not None and a.max < 1:
        ap.error("--max: mindestens 1")  # 0 hiesse sonst "alle"
    try:
        z = ordner_ins_wissen(a.mandant, a.ordner, a.sammlung, a.sichtbarkeit, a.zugehoerigkeit, a.traeger, a.karten, a.max,
                              a.max_seiten, not a.ohne_embed, a.datei or None)
    except bp.LaufLaeuft as ex:  # JSON, rc 2; plugin_api macht daraus 409
        print(json.dumps({"abbruch": str(ex)[:300], "lauf_laeuft": True}, ensure_ascii=False) if a.json else "A run is already active: %s" % ex)
        return 2
    except (bp.ProtokollGesperrt, bp.ProtokollDefekt, ValueError) as ex:
        print(json.dumps({"fehler": str(ex)}, ensure_ascii=False))
        return 3
    if a.json:
        print(json.dumps(z, ensure_ascii=False, default=str))
    else:
        print("%(dateien)d files: %(neu)d new, %(schon_da)d already there, %(karten)d cards, %(fehler)d errors, "
              "%(dubletten)d duplicates" % z)
        print("QMD:", json.dumps(z.get("qmd"), ensure_ascii=False, default=str)[:600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
