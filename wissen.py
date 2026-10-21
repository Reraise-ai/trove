"""Trove F16 — Ordner ins Wissen: Dateien lesen -> maschinenlesbares Markdown je Beleg -> mit dem Original verheiratet
(sha256 + Belegprotokoll) -> QMD-Sammlung (Suche) -> auf Wunsch Beleg-Karten auf dem Brett (Weltenbauer/Reraise)
mit Sichtbarkeit privat oder oeffentlich.

Ablage:  <TROVE_DATEN>\\<mandant>\\wissen\\<sammlung>\\<sha16>_<name>.md   (Kopfdaten: quelle, pruefsumme, Felder,
         Sichtbarkeit; nach erfolgreicher Karte zusaetzlich "karte: b-NNN")
QMD:     Sammlung "trove-<mandant>-<sammlung>" (nur per -c abrufbar: includeByDefault false, bei jedem Lauf geprueft),
         update + embed. Laeuft OHNE Protokoll-Sperre.
Brett:   nur ueber den Pflicht-Schreibkanal dem Schreibkanal (TROVE_BRETT)/_werkzeug/brett.py:
         reserve --brett belege -n 1 -> karte --id=<id> ... -> bei Fehler freigeben --id=<id>. Nie selbst IDs rechnen.
Protokoll (F8): je Datei kurz gesperrt "gelesen" (vor dem Ablegen des Zwillings), "notiz" mit karte=<id> je Karte (Log, keine
         Nachschlagequelle), Start/Ende/QMD als "notiz"; Anker vor und nach QMD. Die Sperre wird nie ueber einen langen Lauf
         gehalten (gemeinsam.prot_mit_wiederholung: bis 60 s warten). ProtokollGesperrt/ProtokollDefekt brechen den Lauf ab.
Karte:   EINE Wahrheit: die Kopfzeile "karte: <id>" im Zwilling. Reihenfolge: Merker setzen (<sammlung>.karten.json:
         sha -> "unterwegs") -> reserve (Merker mit ID) -> karte -> Kopfzeile -> Protokoll-Notiz -> Merker loeschen.
         Der Merker lebt nur waehrend des Laufs; steht beim naechsten Lauf noch ein Eintrag (und keine Kopfzeile) ->
         Fehler "Karte unklar — Brett pruefen (ID ...)", KEINE neue Karte. Kopfzeile vorhanden -> alter Merker wird entfernt.
         Zeitablauf bei brett.py karte: Merker bleibt mit ID, nicht freigeben (die Karte kann geschrieben sein).
Sichtbarkeit: privat = eigene Nutzung (freigabe privat) | oeffentlich = Reraise-Tausch (freigabe community). Pflichtangabe.
         Private Karten: voller Zwilling (gekuerzt), nur ab 200 Zeichen Text. Oeffentliche Karten: nur die Positivliste
         oeffentlich.kartenfelder (nichts aus Belegtext oder Dateiname); privater Zwilling derselben Datei irgendwo -> keine Karte.
         Privater Lauf ueber eine Datei, die schon oeffentlich ist (oeffentlicher Zwilling mit Brett-Karte oder Reraise-Karte):
         Zwilling wird geschrieben, Eintrag "schon_oeffentlich" zaehlt als fehler_datei (Meldung, keine Sperre). Reraise
         "unklar"/"unterwegs" (Karte kann existieren) zaehlt mit ("Reraise unklar").
Dubletten: Dateien gleichen Inhalts (sha256) je Lauf einmal; weitere Fundstellen -> "dublette" (kein Zwilling/Brett), Zaehler.
Zwillingssuche: oeffentlich.zwillings_index einmal je Lauf unter der Laufsperre, neue Zwillinge werden nachgetragen.
Laufsperre je Mandant: gemeinsam.laufsperre (<mandant>\\wissen\\.lauf, dieselbe wie reraise_hochladen -> LaufLaeuft).
lese_treppe/verprobung (OCR-Pakete) werden erst beim Lesen geladen.
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
BRETT = os.environ.get("TROVE_BRETT", "")
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
        # Kundenmodus (wiki): nur der Text-Zwilling, direkt im Wissenssystem (raw/trove/<sammlung>), nichts doppelt
        self.ordner = wk.raw_ziel(self.sammlung) if wk.kartenziel() == "wiki" else os.path.join(bp.WURZEL, self.mandant, "wissen", self.sammlung)


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
    """PDF/Bild ohne pymupdf: diese Datei wird nicht gelesen; Text-, Markdown- und Word-Dateien laufen weiter."""


_FITZ = []


def fitz_da():
    if not _FITZ:
        try:
            import fitz  # noqa: F401
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
            raise ValueError("Word-Datei zu gross (document.xml %d Bytes)" % info.file_size)
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
        raise FitzFehlt("PDF/Bild nicht gelesen: Paket pymupdf fehlt im Python %s (Gesundheitscheck nennt den Befehl). "
                        "Text-, Markdown- und Word-Dateien werden gelesen." % sys.executable)
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
                raise ValueError(".heic nicht gelesen: die Umwandlung braucht das macOS-Werkzeug sips")
            if r.returncode or not os.path.isfile(ziel):
                raise ValueError(".heic nicht gelesen: sips meldet %s" % ((r.stderr or r.stdout).strip()[:200] or "Fehler %d" % r.returncode))
            art = "heic->jpeg"
        else:
            if not zipfile.is_zipfile(pfad):
                raise ValueError("%s nicht gelesen: keine Ein-Datei-Ablage (beschaedigt oder anderes Format)" % typ)
            with zipfile.ZipFile(pfad) as z:
                namen = {n.lower(): n for n in z.namelist()}
                treffer = next((namen[k] for k in IWORK_VORSCHAU if k in namen), None)
                if not treffer:
                    raise ValueError("%s nicht gelesen: keine eingebettete Vorschau im Dokument (im Programm als PDF exportieren)" % typ)
                if z.getinfo(treffer).file_size > MAX_VORSCHAU_BYTES:
                    raise ValueError("%s nicht gelesen: Vorschau zu gross" % typ)
                ziel = os.path.join(tmp, "vorschau" + os.path.splitext(treffer)[1].lower())
                with open(ziel, "wb") as fh:
                    fh.write(z.read(treffer))
            art = "Vorschau " + treffer
        erg = lt.lesen(ziel, "rapidocr", False, max_seiten)
    erg.update(datei=pfad, sha256=sha, methode="%s (%s)" % (erg["methode"], art))
    return erg


LESEKOPIE = "Dies ist eine Lesekopie, nicht die Datei selbst."
KEIN_TEXT = "(kein Text erkannt)"


def original_zeile(pfad):
    """Erste Textzeile jedes Zwillings: wo das Original liegt (absolut). Der Agent soll diesen Pfad nennen, nicht den Zwilling."""
    return "Original liegt bei dir unter: %s" % pfad


def hinweis_kopf(pfad):
    return "%s -- %s" % (original_zeile(pfad), LESEKOPIE)


def lesbarer_text(md_text):
    """Text des Zwillings (Abschnitt '## Text') oder "", wenn nichts Lesbares darin steht (leer, Platzhalter, nur Zeichen ohne Buchstabe/Ziffer)."""
    text = md_text.split("\n## Text\n", 1)[1].strip() if "\n## Text\n" in md_text else ""
    return text if text != KEIN_TEXT and re.search(r"\w", text) else ""


def inhaltszeile_mit_titel(md_text, n=200):
    """'Inhalt: <ganzer Kurztext>' oder 'Inhalt (Anfang): <~n Zeichen> …'; "" ohne lesbaren Text."""
    anfang = inhaltszeile(md_text, n) if lesbarer_text(md_text) else ""
    if not anfang:
        return ""
    return ("Inhalt (Anfang): " if anfang.endswith(" …") else "Inhalt: ") + anfang


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
            raise ValueError("Zwilling %s gehoert nicht zu sha256 %s (Kopf: %s)" % (vorhanden[0], sha, kopf.get("pruefsumme")))
        if kopf.get("sichtbarkeit") != regel["sichtbarkeit"]:
            raise ValueError("Sichtbarkeit weicht ab (vorhanden: %s) in %s" % (kopf.get("sichtbarkeit"), vorhanden[0]))
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
              "Original: `%s`  " % pfad, "Pruefsumme (sha256): `%s`  " % sha,
              "Gelesen: %s, %s Seiten, Verprobung %s%s" % (erg["methode"], erg["seiten"], verprobung, gruende), "",
              "## Felder", ""]
    zeilen += ["- %s: %s" % (k, w) for k, w in f.items() if w not in (None, "")]
    zeilen += ["", "## Text", "", (erg.get("text") or "").strip() or KEIN_TEXT, ""]
    inhalt = "\n".join(zeilen)
    meldung = "Markdown-Zwilling fuer das Wissen (%s, %s Seiten, Verprobung %s, Sichtbarkeit %s)"
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
        raise ValueError("Ordnerpfad enthaelt fuer qmd.cmd verbotene Zeichen (&|<>^%%\"!): %r" % ordner)


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
        raise RuntimeError("qmd %s: Zeit abgelaufen (%d s), Prozessbaum beendet" % (args[0], timeout))
    return p.returncode, (out or "").strip()


def _index_yml(name):
    """includeByDefault: false fuer die Sammlung sicherstellen. Rueckgabe (text, ist_fehler)."""
    try:
        with open(QMD_INDEX_YML, encoding="utf-8") as fh:
            y = fh.read()
    except OSError as ex:
        return "nicht lesbar: " + str(ex)[:120], True
    m = re.search(r"^  %s:\n(?:    .*\n)+" % re.escape(name), y, re.M)
    if not m:
        return "Sammlung nicht gefunden in index.yml", True
    block = m.group(0)
    soll = "true" if os.environ.get("TROVE_QMD_STANDARDSUCHE", "").lower() in ("1", "true", "ja") else "false"  # Kunde: true (Standardsuche findet sie)
    if "    includeByDefault: %s\n" % soll in block:
        return "schon gesetzt", False
    if "includeByDefault" in block:
        return "includeByDefault steht anders als %s — nicht geaendert" % soll, True
    try:
        sik = QMD_INDEX_YML + ".bak-vor-trove-" + time.strftime("%Y%m%d-%H%M%S")
        with open(sik, "w", encoding="utf-8") as fh:
            fh.write(y)
        gm.schreibe_atomar(QMD_INDEX_YML, y.replace(block, block + "    includeByDefault: %s\n" % soll, 1))
    except OSError as ex:
        return "nicht geaendert: " + str(ex)[:120], True
    return "gesetzt (Sicherung %s)" % os.path.basename(sik), False


def qmd_wiki(l):
    """kartenziel wiki: keine eigene Sammlung. Der Zwilling liegt unter <wiki>/../raw/trove/ und wird ueber die vorhandene
    Kunden-Sammlung 'raw' gefunden; hier nur Index + Embeddings der Standardsammlungen auffrischen (sonst gaebe es Doppeltreffer)."""
    import wissenskarte as wk
    erg = {"sammlung": "raw (vorhandene Kunden-Sammlung)", "fehler": 0}
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


def qmd_sammlung(l):
    """Sammlung anlegen (einmal), includeByDefault pruefen (jedes Mal), Index + Embeddings auffrischen."""
    import wissenskarte as wk
    if wk.kartenziel() == "wiki":
        return qmd_wiki(l)
    name = "trove-%s-%s" % (l.mandant, l.sammlung)
    _qmd_pruefen(l.ordner)
    erg = {"sammlung": name, "fehler": 0}
    rc, out = _qmd("collection", "list")
    if rc != 0:
        raise RuntimeError("qmd collection list: rc %d %s" % (rc, out[-300:]))
    if not re.search(r"^\s*%s \(qmd://" % re.escape(name), out, re.M):
        rc, out = _qmd("collection", "add", l.ordner, "--name", name)
        erg["angelegt"] = (rc, out[-300:])
        erg["fehler"] += rc != 0
    erg["index_yml"], ist_fehler = _index_yml(name)
    if ist_fehler:
        erg["fehler"] += 1
        erg["update"] = "nicht ausgefuehrt (index.yml)"
        return erg
    rc, out = _qmd("update", timeout=900)
    erg["update"] = (rc, out[-400:])
    erg["fehler"] += rc != 0
    if l.embed:
        rc, out = _qmd("embed", "-c", name, "--timeout", "20", timeout=1800)
        erg["embed"] = (rc, out[-400:])
        erg["fehler"] += rc != 0
    rc, out = _qmd("ls", name)
    erg["dateien_im_index"] = len([z for z in out.splitlines() if z.strip().lower().endswith(".md")]) if rc == 0 else out[-200:]
    erg["fehler"] += rc != 0
    return erg


def _brett(*args, timeout=300):
    if not BRETT:
        raise RuntimeError("kartenziel brett, aber TROVE_BRETT (Ordner des Bretts) ist nicht gesetzt")
    r = subprocess.run([sys.executable, os.path.join(BRETT, "_werkzeug", "brett.py"), "--sitzung", "trove", *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=BRETT,
                       env={**os.environ, "PYTHONUTF8": "1", "BRETT_URHEBER": "agent:trove"})
    if r.returncode != 0:
        raise RuntimeError("brett.py %s: rc %d %s" % (args[0], r.returncode, (r.stderr or r.stdout)[-500:]))
    return r.stdout.strip()


def _reservieren():
    out = _brett("reserve", "--brett=belege", "-n", "1")
    ids = sorted(set(re.findall(r"\bb-\d+\b", out)))
    if len(ids) != 1:
        raise RuntimeError("brett.py reserve: keine eindeutige ID in der Ausgabe: %r" % out[-300:])
    return ids[0]


def kartentext(kopf, md_text, md, l):
    """Private Karte: voller Zwilling (gekuerzt) mit Verweis auf Zwilling und Sammlung."""
    _, ende = gm.kopf_grenzen(md_text)
    body = md_text[ende + 5:] if ende >= 0 else md_text
    kurz = body[:6000] + ("\n\n(gekuerzt; Volltext im Markdown-Zwilling)\n" if len(body) > 6000 else "")
    kopfzeile = "**Markdown-Zwilling:** `%s`  \n**Trove-Sammlung:** `trove-%s-%s` (QMD, `-c`)  \n**Verprobung:** %s\n\n"
    return kopfzeile % (md, l.mandant, l.sammlung, kopf.get("verprobung")) + kurz


def privat_titel(kopf):
    """Titel privater Karten, nur aus dem Zwilling-Kopf: [Belegdatum ]titel[ — Brutto Waehrung]."""
    if not kopf.get("titel"):
        raise ValueError("Zwilling ohne titel im Kopf")
    t = ("%s " % kopf["belegdatum"] if kopf.get("belegdatum") else "") + str(kopf["titel"])
    if kopf.get("brutto") is not None:
        t += " — %s %s" % (kopf["brutto"], kopf.get("waehrung") or "")
    return t.strip()[:120]


def _karte_im_kopf(md, kid):
    with open(md, encoding="utf-8") as fh:
        text = fh.read()
    _, ende = gm.kopf_grenzen(text)
    if ende < 0:
        raise ValueError("Zwilling ohne Kopfdaten: " + md)
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
    if karte.startswith("wiki:") and wk.kartenziel() == "wiki":
        cp = os.path.join(wk.wiki_wurzel(), *karte[5:].split("/")) + ".md"
        if os.path.exists(cp):
            with open(cp, encoding="utf-8") as fh:
                ct = fh.read()
            nt = ct.replace("Liegt bei dir unter `%s`." % alt, "Liegt bei dir unter `%s`." % neu)
            nt = nt.replace("\nsource: %s\n" % " ".join(alt.split()), "\nsource: %s\n" % " ".join(neu.split()), 1)  # Urheber-Kopf (19.10.)
            if nt != ct:
                gm.schreibe_atomar(cp, nt)
                wk.GEAENDERT.append(cp)
    _notiz(l, werkzeug="wissen.py", quelle=neu, ziel=md, sha256=kopf.get("pruefsumme"),
           meldung="Original umbenannt/verschoben: %s -> %s (Zwilling und Karte nachgezogen)" % (alt, neu))
    return alt


def _merker_pfad(l):
    return l.ordner + ".karten.json"


def merker_lesen(pfad):
    try:
        with open(pfad, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def merker_setzen(l, sha, zustand, kid=None):
    """Einzige Stelle fuer den Merker: zustand "unterwegs" (mit reservierter ID, sobald bekannt) oder None = entfernen.
    Andere Eintraege bleiben stehen. Schreibt nur bei Aenderung."""
    pfad = _merker_pfad(l)
    m = merker_lesen(pfad)
    if zustand is None and sha not in m:
        return
    if zustand is None:
        del m[sha]
    else:
        m[sha] = {"zustand": zustand, "id": kid, "zeit": bp.jetzt()}
    gm.schreibe_atomar(pfad, json.dumps(m, ensure_ascii=False, indent=1))


def karte_erzeugen(l, md_erg, min_zeichen=1):
    """Beleg-Karte aus dem Zwilling, nur ueber brett.py. Wahrheit: Kopfzeile 'karte:'. Zugehoerigkeit/Traeger aus dem Kopf.
    Oeffentlich: nur Positivliste (oeffentlich.kartenfelder); verweigert, wenn dieselbe Datei irgendwo privat ist.
    ProtokollGesperrt/ProtokollDefekt gehen unveraendert an den Aufrufer."""
    regel = sichtbarkeit_regel(l.sichtbarkeit)
    md, sha = md_erg["md"], md_erg["sha256"]
    kopf, md_text = zwilling_lesen(md)
    if kopf.get("karte"):
        merker_setzen(l, sha, None)  # Absturz zwischen Kopfzeile und Merker-Loeschen: Kopfzeile gilt
        return {"karte": kopf["karte"], "grund": "Karte bestand schon"}
    offen = merker_lesen(_merker_pfad(l)).get(sha)
    if offen:
        raise RuntimeError("Karte unklar — Brett pruefen (ID %s); danach den Eintrag in %s entfernen oder 'karte: <id>' in den "
                           "Zwilling schreiben" % (offen.get("id") or "keine", _merker_pfad(l)))
    zugehoerigkeit, traeger = kopf.get("zugehoerigkeit") or ZUG_OFFEN, kopf.get("traeger") or ZUG_OFFEN
    if regel["sichtbarkeit"] == "oeffentlich":
        privat = oe.privat_irgendwo(os.path.join(bp.WURZEL, l.mandant, "wissen"), sha, l.index)
        if privat:
            return {"karte": None, "grund": "in Sammlung %s privat" % ", ".join(privat)}
        k = oe.kartenfelder(sha, kopf)
        titel, quelle, inhalt, datum = k["titel"], k["quelle"], k["body"], k["datum"]
    else:
        text = lesbarer_text(md_text)  # jede Datei mit lesbarem Text bekommt eine Karte, auch eine Ein-Zeilen-Notiz
        if not text or len(text) < min_zeichen:
            return {"karte": None, "grund": "kein lesbarer Text" if not text else "zu wenig Text (%d Zeichen)" % len(text)}
        titel, quelle, inhalt = privat_titel(kopf), str(kopf.get("quelle") or ""), kartentext(kopf, md_text, md, l)
        datum = oe.iso_datum(kopf.get("belegdatum"))
    import wissenskarte as wk
    if wk.kartenziel() == "wiki":  # Kunde: Wissenskarte im Wiki statt Brett
        if regel["sichtbarkeit"] == "privat":  # Karte verweist nur auf die Quelle (raw/trove/...), der Volltext steht dort einmal -> keine Doppeltreffer
            felder = md_text.split("\n## Felder", 1)[1].split("\n## Text", 1)[0].strip() if "\n## Felder" in md_text and "\n## Text" in md_text else ""
            anfang = inhaltszeile_mit_titel(md_text)  # kurze Datei: voller Text ("Inhalt: ..."), sonst Anfang
            inhalt = "Liegt bei dir unter `%s`.\n\n%sGelesen als Beleg-Zwilling (Verprobung: %s). Volltext in der Quelle.%s" % (
                kopf.get("quelle"), (anfang + "\n\n") if anfang else "", kopf.get("verprobung"),
                ("\n\n" + felder) if felder else "")
        kid, neu = wk.karte_schreiben(l.sammlung, md, sha, titel, inhalt, kopf.get("quelle"), zugehoerigkeit)
        _karte_im_kopf(md, kid)
        _notiz(l, quelle=md, ziel=kid, sha256=sha, werkzeug="wissen.py/wiki", karte=kid, sichtbarkeit=l.sichtbarkeit, md_sha256=bp.datei_sha(md),
               meldung="Wissenskarte %s (%s, %s)" % (kid, "neu" if neu else "bestand schon, Kopfzeile nachgetragen", titel[:80]))
        return {"karte": kid, "grund": None, "karte_neu": neu}
    if ZUG_OFFEN in (zugehoerigkeit, traeger):
        return {"karte": None, "grund": "Zugehörigkeit offen — Brett-Karte erst mit Zugehörigkeit"}
    tmp = md + ".karte.tmp"
    with open(tmp, "w", encoding="utf-8", errors="replace", newline="\n") as fh:
        fh.write(inhalt)
    kid = None
    try:
        merker_setzen(l, sha, "unterwegs")
        kid = _reservieren()
        merker_setzen(l, sha, "unterwegs", kid)
        out = _brett("karte", "--id=" + kid, "--brett=belege", "--typ=beleg", "--titel=" + titel,
                     "--quelle=" + quelle, *(["--quelle-datum=" + datum] if datum else []),
                     "--pruefsumme=" + sha, "--bewertung=frei", "--status=entwurf",
                     "--freigabe=" + regel["freigabe"], "--traeger=" + traeger, "--zugehoerigkeit=" + zugehoerigkeit,
                     "--sichtbarkeit=" + regel["sichtbarkeit"], "--klasse=B", "--ebene=belegt",
                     "--herkunft-system=Trove", "--body-datei=" + tmp)
    except subprocess.TimeoutExpired as ex:
        if kid:  # karte lief ab: sie kann geschrieben sein -> nicht freigeben, Merker mit ID bleibt ("Karte unklar")
            raise RuntimeError("Karte unklar — brett.py karte nach %s s abgebrochen (ID %s); Brett pruefen, Merker bleibt"
                               % (ex.timeout, kid)) from ex
        merker_setzen(l, sha, None)  # reserve lief ab: noch keine Karte
        raise
    except Exception as ex:
        if kid:
            try:
                _brett("freigeben", "--id=" + kid)
            except Exception as fx:  # Merker bleibt "unterwegs": naechster Lauf meldet "Karte unklar"
                raise RuntimeError("%s; freigeben %s gescheitert: %s" % (str(ex)[:300], kid, str(fx)[:200])) from ex
        merker_setzen(l, sha, None)  # ohne Karte kein Merker
        raise
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    try:
        _karte_im_kopf(md, kid)
    except Exception as ex:
        raise RuntimeError("Karte %s steht auf dem Brett, Kopfzeile scheiterte (%s) — Merker bleibt, naechster Lauf meldet "
                           "'Karte unklar'" % (kid, str(ex)[:200])) from ex
    _notiz(l, quelle=md, ziel=os.path.join(BRETT, "brett", "belege", kid + ".md"), sha256=sha, werkzeug="wissen.py/brett",
           meldung="Beleg-Karte %s auf dem Brett (%s, Sichtbarkeit %s, %s)" % (kid, titel[:80], l.sichtbarkeit, zugehoerigkeit),
           karte=kid, sichtbarkeit=l.sichtbarkeit, md_sha256=bp.datei_sha(md), brett_ausgabe=out[-300:])
    merker_setzen(l, sha, None)
    return {"karte": kid, "grund": None, "karte_neu": True}


FREIGABE = "macOS-Freigabe nötig (Systemeinstellungen → Datenschutz & Sicherheit → Festplattenvollzugriff)"


def unlesbar_meldung(pfad):
    return "Ordner nicht lesbar: %s – %s" % (pfad, FREIGABE)


def unter_einem(p, ordner):
    """Liegt p in (oder ist p) einem der Ordner?"""
    return any(_unter(p, _norm(o)) for o in ordner)


def _echt(p):
    return _norm(os.path.realpath(os.path.expanduser(p)))


def eigene_ablagen(mandant):
    """Ordner, die Trove selbst beschreibt und darum nie einliest (sonst liest Trove seine eigenen Karten, Lauf fuer Lauf mehr):
    Wissensspeicher ~/Wissen, Wiki-Wurzel und deren Elternordner (raw/trove, wiki/quellen, wiki/log.md), Datenwurzel.
    Liefert [(normierter echter Pfad, Anzeigepfad, art)]; art "wissen" = nie einlesen, "daten" = Datenwurzel."""
    zonen = [(os.path.join(os.path.expanduser("~"), "Wissen"), "wissen")]
    wiki = os.environ.get("TROVE_WIKI")
    if wiki:
        wiki = os.path.expanduser(wiki).rstrip("/\\") or wiki
        zonen += [(wiki, "wissen"), (os.path.dirname(wiki), "wissen")]
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
        t = (os.path.splitext(p)[1].lower() or "(ohne Endung)") + (" (Paket-Ordner)" if os.path.isdir(p) else "")
        typen[t] = typen.get(t, 0) + 1
    return "nicht gelesen: %d %s, Typen %s" % (len(liste), "Datei" if len(liste) == 1 else "Dateien",
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
            warn.append("Der gewählte Ordner ist dein Wissensspeicher (%s) – Trove liest ihn nicht ein, sonst läse es seine eigenen Karten." % anzeige)
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
                warn.append("Übersprungen: %s ist Trove-eigene Ablage (%s) und wird nie eingelesen." % (os.path.join(r, x), treffer))
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
            raise ValueError("Datei liegt nicht im gewählten Ordner: %s" % p)
        if not os.path.isfile(p):
            hinweise.append("Nicht gefunden oder kein Dateiinhalt: %s" % p)
            continue
        treffer = next((a for s, a in sperren if _unter(_echt(p), s)), None)
        if treffer:
            hinweise.append("Übersprungen: %s ist Trove-eigene Ablage (%s) und wird nie eingelesen." % (p, treffer))
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
        raise ValueError("oeffentlich braucht eine Zugehoerigkeit (nicht offen) — abgebrochen")
    if regel["sichtbarkeit"] == "oeffentlich" and "privat" in zugehoerigkeit.lower():
        raise ValueError("oeffentlich mit privater Zugehoerigkeit (%s) — abgebrochen" % zugehoerigkeit)
    if not os.path.isdir(wurzel):
        raise ValueError("kein Ordner: %s" % wurzel)
    os.makedirs(l.ordner, exist_ok=True)
    with gm.laufsperre(l.mandant):
        for muster in ("*.md.teil", "*.md.karte.tmp"):
            for rest in glob.glob(os.path.join(glob.escape(l.ordner), muster)):
                os.remove(rest)
        l.index = oe.zwillings_index(os.path.dirname(l.ordner))  # Brett: <daten>\\<mandant>\\wissen, Wiki: raw/trove
        return _lauf(l)


def _reraise_karten(mandant):
    """Reraise-Stand (karten/unklar/unterwegs) aus reraise.json und Protokoll ("versendet"); einmal je privatem Lauf."""
    import reraise_hochladen as rh
    return rh.stand_lesen(mandant)


def schon_oeffentlich(l, sha, md, reraise):
    """Privater Lauf: anderer Zwilling derselben Datei ist oeffentlich UND (Brett-Karte im Kopf ODER Reraise-Karte ODER Reraise
    unklar/unterwegs) -> Meldetext mit Karten-/Reraise-ID, sonst None. reraise: Stand wie _reraise_karten ({} = unlesbar)."""
    funde, rk = [], reraise.get("karten", {})
    offen = sha in reraise.get("unklar", {}) or sha in reraise.get("unterwegs", {})
    for s, anderer, kopf, _ in oe.zwillinge(os.path.join(bp.WURZEL, l.mandant, "wissen"), sha, l.index):
        if anderer == md or kopf.get("pruefsumme") != sha or kopf.get("sichtbarkeit") != "oeffentlich":
            continue
        ids = (["Karte %s" % kopf["karte"]] if kopf.get("karte") else []) + \
              (["Reraise %s" % rk[sha].get("id")] if sha in rk else ["Reraise unklar"] if offen else [])
        if ids:
            funde.append("Sammlung %s (%s)" % (s, ", ".join(ids)))
    return ("bereits oeffentlich: " + "; ".join(funde)) if funde else None


def wolke_hinweis(n):
    """Meldung fuer den Tab nach 'Ins Wissen': Dateien, die macOS nur in der Wolke haelt (dataless/.icloud), wurden nicht gelesen."""
    if not n:
        return None
    return "%d %s nur in der Cloud – im Finder ‚Jetzt laden‘, dann erneut „Ins Wissen“." % (n, "Datei" if n == 1 else "Dateien")


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
         "merker_offen": len(merker_lesen(_merker_pfad(l))), "schon_oeffentlich": 0, "fehler_datei": 0, "fehler_karte": 0,
         "fehler_qmd": 0, "dubletten": 0, "fehler": 0, "eintraege": [], "unlesbar": unlesbar,
         "hinweise": [unlesbar_meldung(u) for u in unlesbar] + hinweise + ([ng_hinweis] if ng_hinweis else []),
         "eigene_ablage": bool(hinweise) and not dateien and not l.nur_dateien, "auswahl": len(l.nur_dateien or []), "nicht_gelesen": nicht_gelesen[:200], "nicht_gelesen_anzahl": len(nicht_gelesen)}
    import wissenskarte as wk
    wk.GEAENDERT.clear()
    reraise, erste = None, {}  # erste: sha256 -> erste Fundstelle in diesem Lauf
    _notiz(l, werkzeug="wissen.py", meldung="%s ins Wissen Start: %s -> Sammlung %s (%d Dateien, Sichtbarkeit %s, %s, "
           "%d Merker offen)" % ("Auswahl (%d gewählt)" % len(l.nur_dateien) if l.nur_dateien else "Ordner", l.wurzel, l.sammlung,
                                 len(dateien), l.sichtbarkeit, l.zugehoerigkeit, z["merker_offen"]))
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
            _notiz(l, "fehler", quelle=p, werkzeug="wissen.py", meldung="Lesen/Markdown: " + str(ex)[:300])
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
                e["fehler"] = "Karte: " + str(ex)[:200]
                _notiz(l, "fehler", quelle=p, werkzeug="wissen.py/brett", meldung="Karte: " + str(ex)[:300])
        if l.sichtbarkeit == "privat":
            if reraise is None:
                try:
                    reraise = _reraise_karten(l.mandant)
                except (bp.ProtokollGesperrt, bp.ProtokollDefekt):
                    raise
                except Exception as ex:  # Stand unlesbar: Brett-Karten werden trotzdem geprueft
                    reraise, z["reraise_stand_fehler"] = {}, str(ex)[:200]
            grund = schon_oeffentlich(l, e["sha256"], e["md"], reraise)
            if grund:
                e.update(zustand="schon_oeffentlich", grund=grund + ("; " + e["grund"] if e.get("grund") else ""))
                z["schon_oeffentlich"] += 1
                z["fehler_datei"] += 1
                _notiz(l, "fehler", quelle=p, sha256=e["sha256"], werkzeug="wissen.py", meldung="Privat eingelesen, " + grund)
        z["eintraege"].append(e)
    _notiz(l, anker=True, werkzeug="wissen.py", meldung="Dateien fertig, QMD folgt: %d neu, %d schon da, %d Karten"
           % (z["neu"], z["schon_da"], z["karten"]))
    try:
        z["qmd"] = qmd_sammlung(l)
        z["fehler_qmd"] += z["qmd"]["fehler"]
    except Exception as ex:
        z["qmd"] = {"fehler": 1, "meldung": str(ex)[:300]}
        z["fehler_qmd"] += 1
    z["fehler"] = z["fehler_datei"] + z["fehler_karte"] + z["fehler_qmd"] + len(unlesbar)
    if z.get("ohne_pymupdf"):
        z["hinweise"].append("%d PDF-/Bild-Datei(en) nicht gelesen: Paket pymupdf fehlt (Python %s). Text-, Markdown- und Word-Dateien "
                             "wurden gelesen; nach dem Einrichten von pymupdf erneut „Ins Wissen“ starten." % (z["ohne_pymupdf"], sys.executable))
    zusammen = json.dumps({k: v for k, v in z.items() if k not in ("eintraege", "qmd")}, ensure_ascii=False)
    with _prot(l.mandant) as prot:
        prot.schreiben("notiz" if not z["fehler_qmd"] else "fehler", werkzeug="wissen.py/qmd", mandant=l.mandant, lauf=l.name,
                       ziel=l.ordner, meldung="QMD-Sammlung %s: %s Dateien im Index" % (z["qmd"].get("sammlung"),
                       z["qmd"].get("dateien_im_index")), qmd=json.loads(json.dumps(z["qmd"], default=str)))
        prot.schreiben("notiz", werkzeug="wissen.py", mandant=l.mandant, lauf=l.name, meldung="Ordner ins Wissen Ende: " + zusammen)
        prot.anker_setzen(l.mandant, l.name)
    if wk.kartenziel() == "wiki":
        try:
            z["log"] = wk.log_lauf()  # eine Zeile je Lauf in wiki/log.md
        except Exception as ex:
            z["log_fehler"] = str(ex)[:200]
    if l.karten and z["karten"] and wk.kartenziel() != "wiki":
        try:
            _brett("log", "Trove: %d Beleg-Karten aus Sammlung %s, Sichtbarkeit %s" % (z["karten"], l.sammlung, l.sichtbarkeit))
        except Exception as ex:
            z["brett_log_fehler"] = str(ex)[:200]
    return z


FACTURA_B = """FACTURA B  N° 0003-00012345
Fecha 01/03/2026
Ejemplo SA  CUIT 30-11111111-1  Ingresos Brutos 901-000000-0
Cliente: Juan Perez, Av. Rivadavia 1234, Piso 3, CABA
CUIT 20-12345678-9  DNI N.º 12.345.678  Tel 11 4567-8901
Alias juan.perez.mp  CBU 0170099220000067797370  juan@example.com
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
GEHEIM = ("Juan", "Perez", "Rivadavia", "Piso", "12345678", "12.345.678", "4567", "juan.perez", "0170099", "example", "@",
          "Muster", "Bochum", "0234", "1234567", "7654321", "350/123", "12 345 678 901", "1234567890", "43050001", "3704",
          "Maria", "Lopez", "Falsa", "27-23456789-4", "20-12345678-9", "901-000000-0", "consultoria", "Beratung", "piano",
          "0003-00012345", "30-11111111-1", "RE-2026-0042", "DE298765430", "factura", "rechnung", "recibo", ".txt", "eingang")


def _selbsttest():
    """Der Selbsttest prueft das Kartenziel brett (Attrappe fuer brett.py), auch wenn trove.json kartenziel wiki setzt."""
    vorher = os.environ.get("TROVE_KARTENZIEL")
    os.environ["TROVE_KARTENZIEL"] = "brett"
    try:
        return _selbsttest_brett()
    finally:
        if vorher is None:
            os.environ.pop("TROVE_KARTENZIEL", None)
        else:
            os.environ["TROVE_KARTENZIEL"] = vorher


def _selbsttest_brett():
    """Ohne brett.py, qmd, OCR und verprobung (Attrappen nur im Speicher): Karten-Einzelfaelle und ordner_ins_wissen ganz."""
    import tempfile
    g = globals()
    alt = {k: g[k] for k in ("_brett", "_verproben", "qmd_sammlung", "_prot", "_karte_im_kopf", "_qmd")}, bp.WURZEL
    aufrufe, bodies, merker_beim = [], {}, []
    stoerung = {}

    def brett(*args, timeout=300):
        aufrufe.append(args[0])
        if args[0] in ("reserve", "karte"):
            merker_beim.append((args[0], merker_lesen(stoerung["merker"]) if "merker" in stoerung else None))
        if args[0] == "reserve":
            return "reserviert b-%03d" % (aufrufe.count("reserve") + 6)
        if args[0] == "karte":
            if stoerung.get("karte"):
                raise stoerung["karte"]
            a = dict(x[2:].split("=", 1) for x in args[1:] if x.startswith("--"))
            with open(a["body-datei"], encoding="utf-8") as fh:
                bodies[a["id"]] = {**{k: v for k, v in a.items() if k != "body-datei"}, "body": fh.read()}
        return "ok"

    def verproben(f):  # wie verprobung: sicher nur, wenn netto + steuer = brutto
        if None in (f.get("netto"), f.get("steuer"), f.get("brutto")):
            return ("bestanden" if f.get("netto") else "prueflist"), []
        return ("sicher" if abs(f["netto"] + f["steuer"] - f["brutto"]) < 0.01 else "prueflist"), []

    def prot(m):
        stoerung["prot_aufrufe"] = stoerung.get("prot_aufrufe", 0) + 1
        if stoerung.get("prot_ab") == stoerung["prot_aufrufe"]:  # genau einmal: ein verschluckter Fehler faellt sonst nicht auf
            raise bp.ProtokollGesperrt("Test: Protokoll belegt")
        return alt[0]["_prot"](m)
    g.update(_brett=brett, _verproben=verproben, _prot=prot,
             qmd_sammlung=lambda l: {"sammlung": "trove-%s-%s" % (l.mandant, l.sammlung), "fehler": 0, "dateien_im_index": 3})
    with tempfile.TemporaryDirectory() as w:
        bp.WURZEL = os.path.join(w, "trove")
        try:
            l = Lauf(mandant="t", sammlung="oeff", sichtbarkeit="oeffentlich", zugehoerigkeit="Ejemplo SA", traeger="Firma")
            stoerung["merker"] = _merker_pfad(l)

            def zwilling(sha, sammlung, sicht, kopfzeilen=""):
                md = os.path.join(bp.WURZEL, "t", "wissen", sammlung, sha[:16] + "_r.md")
                gm.schreibe_atomar(md, ('---\ntitel: "Juan Perez DNI 12.345.678.pdf"\nquelle: "C:\\\\eingang\\\\r.pdf"\n'
                                        'pruefsumme: "%s"\nsichtbarkeit: "%s"\nzugehoerigkeit: "Ejemplo SA"\ntraeger: "Firma"\n'
                                        'verprobung: "bestanden"\nbrutto: 1210\n%s---\n\n## Text\n\n%s\n')
                                   % (sha, sicht, kopfzeilen, FACTURA_B))
                return {"md": md, "sha256": sha}
            sha = "d" * 64
            erg = zwilling(sha, "oeff", "oeffentlich")
            priv = zwilling(sha, "priv", "privat")["md"]
            e = karte_erzeugen(l, erg)
            assert e == {"karte": None, "grund": "in Sammlung priv privat"} and not aufrufe, (e, aufrufe)
            os.remove(priv)
            merker_setzen(l, sha, "unterwegs", "b-003")  # Rest eines abgestuerzten Laufs
            try:
                karte_erzeugen(l, erg)
                raise AssertionError("unterwegs nicht gemeldet")
            except RuntimeError as ex:
                assert "Karte unklar" in str(ex) and "b-003" in str(ex) and not aufrufe, ex
            merker_setzen(l, sha, None)
            stoerung["karte"] = RuntimeError("brett.py karte: rc 1")
            try:
                karte_erzeugen(l, erg)
                raise AssertionError("Fehler von brett karte verschluckt")
            except RuntimeError:
                pass
            assert aufrufe == ["reserve", "karte", "freigeben"] and merker_lesen(_merker_pfad(l)) == {}, aufrufe
            stoerung.pop("karte")
            merker_beim.clear()
            e = karte_erzeugen(l, erg)
            assert e == {"karte": "b-008", "grund": None, "karte_neu": True}, e
            # Merker "unterwegs" steht vor reserve (ohne ID) und vor karte (mit ID)
            assert merker_beim == [("reserve", {sha: {"zustand": "unterwegs", "id": None, "zeit": merker_beim[0][1][sha]["zeit"]}}),
                                   ("karte", {sha: {"zustand": "unterwegs", "id": "b-008", "zeit": merker_beim[1][1][sha]["zeit"]}})], merker_beim
            assert merker_lesen(_merker_pfad(l)) == {} and zwilling_lesen(erg["md"])[0]["karte"] == "b-008"
            b = bodies["b-008"]
            assert b["body"] == "verprobung: bestanden\npruefsumme: " + sha and b["titel"] == "Beleg " + sha[:16], b
            assert b["quelle"] == "Trove-Beleg sha256 " + sha and "quelle-datum" not in b, b
            merker_setzen(l, sha, "unterwegs", "b-008")  # Absturz nach der Kopfzeile: Kopfzeile gilt, Merker wird entfernt
            assert karte_erzeugen(l, erg)["grund"] == "Karte bestand schon" and merker_lesen(_merker_pfad(l)) == {}
            # Kopfzeile scheitert: Merker bleibt mit ID, naechster Versuch "Karte unklar", keine zweite Karte
            sha2 = "e" * 64
            erg2 = zwilling(sha2, "oeff", "oeffentlich")
            g["_karte_im_kopf"] = lambda md, kid: (_ for _ in ()).throw(OSError("Platte voll"))
            try:
                karte_erzeugen(l, erg2)
                raise AssertionError("Kopfzeilenfehler verschluckt")
            except RuntimeError as ex:
                assert "b-009" in str(ex), ex
            g["_karte_im_kopf"] = alt[0]["_karte_im_kopf"]
            assert merker_lesen(_merker_pfad(l))[sha2]["id"] == "b-009"
            n = len(aufrufe)
            try:
                karte_erzeugen(l, erg2)
                raise AssertionError("zweite Karte nach Kopfzeilenfehler")
            except RuntimeError as ex:
                assert "Karte unklar" in str(ex) and "b-009" in str(ex) and len(aufrufe) == n, ex
            # Protokoll gesperrt nach der Kopfzeile: geht an den Aufrufer, Kopfzeile steht
            sha3 = "f" * 64
            erg3 = zwilling(sha3, "oeff", "oeffentlich")
            stoerung.update(prot_aufrufe=0, prot_ab=1)
            try:
                karte_erzeugen(l, erg3)
                raise AssertionError("ProtokollGesperrt verschluckt")
            except bp.ProtokollGesperrt:
                pass
            stoerung.pop("prot_ab")
            assert zwilling_lesen(erg3["md"])[0]["karte"] == "b-010"
            assert karte_erzeugen(l, erg3)["grund"] == "Karte bestand schon" and sha3 not in merker_lesen(_merker_pfad(l))
            # privat: ohne lesbaren Text keine Karte, kurzer Text (4 Zeichen) bekommt eine; oeffentlich braucht keinen Text
            lp = Lauf(mandant="t", sammlung="kurz", sichtbarkeit="privat", zugehoerigkeit="Muster privat", traeger="Person")
            md = os.path.join(lp.ordner, "1" * 16 + "_r.md")
            kurz = '---\ntitel: "r.pdf"\npruefsumme: "%s"\nsichtbarkeit: "SICHT"\nzugehoerigkeit: "X"\ntraeger: "Person"\n---\n\n## Text\n\nkurz\n'
            gm.schreibe_atomar(md, kurz.replace("SICHT", "privat") % ("1" * 64))
            assert karte_erzeugen(lp, {"md": md, "sha256": "1" * 64})["karte"]
            md0 = os.path.join(lp.ordner, "0" * 16 + "_leer.md")
            gm.schreibe_atomar(md0, kurz.replace("SICHT", "privat").replace("\nkurz\n", "\n" + KEIN_TEXT + "\n") % ("0" * 64))
            assert karte_erzeugen(lp, {"md": md0, "sha256": "0" * 64}) == {"karte": None, "grund": "kein lesbarer Text"}
            md = os.path.join(l.ordner, "2" * 16 + "_r.md")
            gm.schreibe_atomar(md, kurz.replace("SICHT", "oeffentlich") % ("2" * 64))
            assert karte_erzeugen(l, {"md": md, "sha256": "2" * 64})["karte_neu"]
            # ordner_ins_wissen ganz: drei Belege (.txt ueber die echte lese_treppe.felder_aus_text), Karten oeffentlich
            quelle = os.path.join(w, "eingang")
            for n_, t in (("factura_b.txt", FACTURA_B), ("rechnung_de.txt", RECHNUNG_DE), ("recibo.txt", RECIBO_PERSONA)):
                gm.schreibe_atomar(os.path.join(quelle, n_), t)
            bodies.clear()
            stoerung["merker"] = os.path.join(bp.WURZEL, "t", "wissen", "belege.karten.json")
            z = ordner_ins_wissen("t", quelle, "belege", "oeffentlich", "Ejemplo SA", karten=True)
            assert (z["dateien"], z["neu"], z["karten"], z["fehler"], z["merker_offen"]) == (3, 3, 3, 0, 0), z
            alles = json.dumps(bodies, ensure_ascii=False)
            for x in GEHEIM:
                assert x not in alles, (x, alles[max(0, alles.find(x) - 80):alles.find(x) + 40])
            titel = sorted(b["titel"] for b in bodies.values())
            ohne_datum = [t for t in titel if re.fullmatch(r"Beleg [0-9a-f]{16}", t)]  # prueflist: kein Datum (E-C3)
            assert len(ohne_datum) == 1 and sorted(set(titel) - set(ohne_datum)) == ["Beleg 2026-03-01 1210.00 ARS",
                                                                                     "Beleg 2026-03-05 (bestanden)"], titel
            assert all("2026-03-02" not in json.dumps(b) for b in bodies.values()), bodies
            for b in bodies.values():
                betraege = any(z_.split(":")[0] in oe.BETRAEGE for z_ in b["body"].split("\n"))
                assert betraege == ("verprobung: sicher" in b["body"]), b  # keine Betraege bei bestanden/prueflist
                assert b["sichtbarkeit"] == "oeffentlich" and b["freigabe"] == "community", b
            z2 = ordner_ins_wissen("t", quelle, "belege", "oeffentlich", "Ejemplo SA", karten=True)
            assert (z2["schon_da"], z2["karten"], z2["fehler"]) == (3, 0, 0), z2
            r = bp.pruefen(bp.pfad_fuer("t"), bp.letzter_anker("t"))
            assert r["kette"] == "ok" and r["anker"] == "ok", r
            ev = [(e["ereignis"], e["werkzeug"]) for e in bp.eintraege(bp.pfad_fuer("t"))]
            assert ev.count(("gelesen", "wissen.py")) == 3 and ev.count(("notiz", "wissen.py/brett")) == 6, ev  # 6: auch die Kurznotiz "kurz" hat jetzt eine Karte
            # dieselbe Sammlung mit anderer Sichtbarkeit: jede Datei ein Fehler, nichts als "schon da" uebernommen
            z3 = ordner_ins_wissen("t", quelle, "belege", "privat", "Ejemplo SA")
            assert (z3["schon_da"], z3["fehler_datei"]) == (0, 3), z3
            assert all("Sichtbarkeit weicht ab" in e["fehler"] for e in z3["eintraege"]), z3
            # U3: privat eingelesen, aber schon oeffentlich (Brett-Karte): gemeldet als Fehler, Zwilling trotzdem geschrieben
            globs = []
            alt_glob = glob.glob
            glob.glob = lambda *a, **k: globs.append(a) or alt_glob(*a, **k)
            try:
                z4 = ordner_ins_wissen("t", quelle, "privkopie", "privat", "Ejemplo SA")
            finally:
                glob.glob = alt_glob
            assert (z4["neu"], z4["schon_oeffentlich"], z4["fehler_datei"]) == (3, 3, 3), z4
            assert all(e["zustand"] == "schon_oeffentlich" and "Sammlung belege (Karte b-" in e["grund"] and os.path.exists(e["md"])
                       for e in z4["eintraege"]), z4
            assert len(globs) == 2, globs  # E-A6: nur die zwei Aufraeum-Muster, Zwillingssuche ueber den Index
            # U3 ueber Reraise (oeffentlicher Zwilling ohne Brett-Karte, sha in reraise.json) und Gegenprobe ohne beides
            quelle3 = os.path.join(w, "eingang3")
            for n_ in ("gesendet.txt", "nur_oeffentlich.txt"):
                gm.schreibe_atomar(os.path.join(quelle3, n_), "Beleg " + n_)
            ordner_ins_wissen("t", quelle3, "oeff3", "oeffentlich", "Ejemplo SA")
            sha_r = bp.datei_sha(os.path.join(quelle3, "gesendet.txt"))
            gm.schreibe_atomar(os.path.join(bp.WURZEL, "t", "reraise.json"),
                               json.dumps({"karten": {sha_r: {"id": "rc-1"}}, "unklar": {}, "unterwegs": {}}))
            z5 = ordner_ins_wissen("t", quelle3, "priv3", "privat", "Ejemplo SA")
            zu = {os.path.basename(e["datei"]): (e["zustand"], e["grund"]) for e in z5["eintraege"]}
            assert zu == {"gesendet.txt": ("schon_oeffentlich", "bereits oeffentlich: Sammlung oeff3 (Reraise rc-1)"),
                          "nur_oeffentlich.txt": ("neu", None)} and z5["fehler_datei"] == 1, z5
            # H7: Reraise "unklar"/"unterwegs" meldet ebenfalls (Karte kann existieren)
            for zst in ("unklar", "unterwegs"):
                gm.schreibe_atomar(os.path.join(bp.WURZEL, "t", "reraise.json"),
                                   json.dumps({"karten": {}, "unklar": {}, "unterwegs": {}, zst: {sha_r: "zeit"}}))
                z5 = ordner_ins_wissen("t", quelle3, "priv3", "privat", "Ejemplo SA")
                zu = {os.path.basename(e["datei"]): (e["zustand"], e["grund"]) for e in z5["eintraege"]}
                assert zu["gesendet.txt"] == ("schon_oeffentlich", "bereits oeffentlich: Sammlung oeff3 (Reraise unklar)"), (zst, z5)
            os.remove(os.path.join(bp.WURZEL, "t", "reraise.json"))
            # S1/H1: Dublette im Lauf -> einmal gelesen, eine Karte, zweite Fundstelle "dublette" ohne Brett
            quelle4 = os.path.join(w, "eingang4")
            for n_ in ("a.txt", "b.txt"):
                gm.schreibe_atomar(os.path.join(quelle4, n_), FACTURA_B + "\ndublette")
            n = len(aufrufe)
            z6 = ordner_ins_wissen("t", quelle4, "dup", "oeffentlich", "Ejemplo SA", karten=True)
            assert [e["zustand"] for e in z6["eintraege"]] == ["neu", "dublette"] and (z6["dubletten"], z6["karten"]) == (1, 1) \
                and aufrufe[n:] == ["reserve", "karte", "log"] and z6["eintraege"][1]["grund"] == \
                "gleicher Inhalt wie " + os.path.join(quelle4, "a.txt"), (z6, aufrufe[n:])
            # U6: Zeitablauf bei brett.py karte -> nicht freigeben, Merker mit ID bleibt, naechster Versuch "Karte unklar"
            sha6 = "6" * 64
            erg6 = zwilling(sha6, "oeff", "oeffentlich")
            del aufrufe[:]
            stoerung["karte"] = subprocess.TimeoutExpired(["brett.py", "karte"], 300)
            try:
                karte_erzeugen(l, erg6)
                raise AssertionError("Zeitablauf verschluckt")
            except RuntimeError as ex:
                meldung6 = str(ex)
                assert "Karte unklar" in meldung6 and aufrufe == ["reserve", "karte"], (ex, aufrufe)
            stoerung.pop("karte")
            kid6 = merker_lesen(_merker_pfad(l))[sha6]["id"]
            assert kid6 and kid6 in meldung6, merker_lesen(_merker_pfad(l))
            try:
                karte_erzeugen(l, erg6)
                raise AssertionError("zweite Karte nach Zeitablauf")
            except RuntimeError as ex:
                assert "Karte unklar" in str(ex) and kid6 in str(ex) and aufrufe == ["reserve", "karte"], (ex, aufrufe)
            try:
                ordner_ins_wissen("t", quelle, "x1", "oeffentlich", "Muster privat")
                raise AssertionError("oeffentlich mit privater Zugehoerigkeit angenommen")
            except ValueError as ex:
                assert "privat" in str(ex), ex
            # Quellbaum = Mandantenordner: wissen\ wird ausgelassen, Zwillinge (erzeugt_von) gezaehlt und nicht gelesen
            gm.schreibe_atomar(os.path.join(bp.WURZEL, "t", "wissen", "belege", "rest.txt"), "x")
            gm.schreibe_atomar(os.path.join(bp.WURZEL, "t", "kopie", "z.md"), '---\nerzeugt_von: "%s"\n---\n' % ERZEUGT_VON)
            gm.schreibe_atomar(os.path.join(bp.WURZEL, "t", "kopie", "notiz.md"), "eigene Notiz")
            gm.schreibe_atomar(os.path.join(bp.WURZEL, "t", "kopie", ".beleg.pdf.icloud"), "plist")  # iCloud-Platzhalter
            d, nz, wolke = dateien_sammeln("t", os.path.join(bp.WURZEL, "t"))
            assert d == [os.path.join(bp.WURZEL, "t", "kopie", "notiz.md")] and nz == 1, (d, nz)
            assert [os.path.basename(w) for w in wolke] == [".beleg.pdf.icloud"], wolke  # gezaehlt, nicht gelesen
            # M33: reserve muss genau eine ID liefern
            for aus, ok in (("reserviert b-001 b-002", False), ("reserviert b-001 (b-001)", True), ("nichts", False)):
                g["_brett"] = lambda *a, **k: aus
                try:
                    assert _reservieren() == "b-001" and ok, aus
                except RuntimeError:
                    assert not ok, aus
            g["_brett"] = brett
            # BatBadBut: verbotene Zeichen im Sammlungspfad -> ValueError, qmd wird nie aufgerufen
            qmd_aufrufe = []
            g["_qmd"] = lambda *a, **k: qmd_aufrufe.append(a) or (0, "")
            lq = Lauf(mandant="t", sammlung="qmd", sichtbarkeit="privat", zugehoerigkeit="X", traeger="Firma")
            for c in sorted(QMD_VERBOTEN):
                lq.ordner = os.path.join(w, "a" + c + "b")
                try:
                    alt[0]["qmd_sammlung"](lq)
                    raise AssertionError("qmd mit %r im Pfad aufgerufen" % c)
                except ValueError:
                    pass
            assert not qmd_aufrufe and _qmd_pruefen(os.path.join(w, "sauber")) is None, qmd_aufrufe
            # ProtokollGesperrt bricht den Lauf ab (Lesen und Karte), wird nie als Dateifehler gezaehlt
            quelle2 = os.path.join(w, "eingang2")
            gm.schreibe_atomar(os.path.join(quelle2, "neu.txt"), RECHNUNG_DE + "\nneu")
            stoerung.update(prot_aufrufe=0, prot_ab=2)  # 1 = Start-Notiz, 2 = "gelesen"
            try:
                ordner_ins_wissen("t", quelle2, "neu", "oeffentlich", "Ejemplo SA", karten=True)
                raise AssertionError("ProtokollGesperrt beim Lesen verschluckt")
            except bp.ProtokollGesperrt:
                pass
            stoerung.pop("prot_ab")
            stoerung["karte"] = bp.ProtokollGesperrt("Test")
            try:
                ordner_ins_wissen("t", quelle2, "neu", "oeffentlich", "Ejemplo SA", karten=True)
                raise AssertionError("ProtokollGesperrt bei der Karte verschluckt")
            except bp.ProtokollGesperrt:
                pass
            stoerung.pop("karte")
            try:
                ordner_ins_wissen("t", quelle, "Belege", "privat", "Ejemplo SA")
                raise AssertionError("Sammlungsname nicht geprueft")
            except ValueError:
                pass
            with gm.laufsperre("t"):  # dieselbe Sperre wie reraise_hochladen
                try:
                    ordner_ins_wissen("t", quelle, "belege", "oeffentlich", "Ejemplo SA")
                    raise AssertionError("zweiter Lauf nicht gesperrt")
                except bp.LaufLaeuft:
                    pass
        finally:
            g.update(alt[0])
            bp.WURZEL = alt[1]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Trove F16 Ordner ins Wissen (Markdown, QMD, Karten)")
    ap.add_argument("ordner", nargs="?")
    ap.add_argument("--mandant", default=bp.konfig.mandant())
    ap.add_argument("--sammlung", default=SAMMLUNG_STANDARD, help="Name der Sammlung (a-z 0-9 - _), Standard quellen")
    ap.add_argument("--sichtbarkeit", default="privat", help="privat = eigene Nutzung, oeffentlich = Reraise-Tausch")
    ap.add_argument("--zugehoerigkeit", help="z. B. 'Meine Firma GmbH' oder 'Mein privat'")
    ap.add_argument("--traeger", choices=["Person", "Firma", "Projekt", "Kunde", "Behoerde"],
                    help="ohne Angabe: Person, wenn 'privat' in der Zugehoerigkeit steht, sonst Firma")
    ap.add_argument("--karten", action="store_true", help="Beleg-Karten auf dem Brett erzeugen (brett.py)")
    ap.add_argument("--max", type=int, help="hoechstens so viele Dateien (Probe)")
    ap.add_argument("--datei", action="append", default=[], help="nur diese Datei(en) unter ordner (mehrfach); sonst ganzer Ordner")
    ap.add_argument("--max-seiten", type=int, default=30)
    ap.add_argument("--ohne-embed", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selbsttest", action="store_true")
    a = ap.parse_args()
    if a.selbsttest:
        _selbsttest()
        gm.selbsttest()
        oe.selbsttest()
        print("selbsttest ok")
        return 0
    if not a.ordner:
        ap.error("ordner angeben (--sammlung, --sichtbarkeit, --zugehoerigkeit optional)")
    if a.max is not None and a.max < 1:
        ap.error("--max: mindestens 1")  # 0 hiesse sonst "alle"
    try:
        z = ordner_ins_wissen(a.mandant, a.ordner, a.sammlung, a.sichtbarkeit, a.zugehoerigkeit, a.traeger, a.karten, a.max,
                              a.max_seiten, not a.ohne_embed, a.datei or None)
    except bp.LaufLaeuft as ex:  # wie reraise_hochladen: JSON, rc 2; plugin_api macht daraus 409
        print(json.dumps({"abbruch": str(ex)[:300], "lauf_laeuft": True}, ensure_ascii=False) if a.json else "Lauf laeuft bereits: %s" % ex)
        return 2
    except (bp.ProtokollGesperrt, bp.ProtokollDefekt, ValueError) as ex:
        print(json.dumps({"fehler": str(ex)}, ensure_ascii=False))
        return 3
    if a.json:
        print(json.dumps(z, ensure_ascii=False, default=str))
    else:
        print("%(dateien)d Dateien: %(neu)d neu, %(schon_da)d schon da, %(karten)d Karten, %(fehler)d Fehler, "
              "%(merker_offen)d Merker offen, %(dubletten)d Dubletten" % z)
        print("QMD:", json.dumps(z.get("qmd"), ensure_ascii=False, default=str)[:600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
