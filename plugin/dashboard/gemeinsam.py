"""Trove F16 — gemeinsame Helfer fuer wissen.py und reraise_hochladen.py (nur Standardbibliothek, kein OCR).

Namenspruefung Mandant/Sammlung, Zwilling-Kopf schreiben (yaml_wert) und lesen (kopf_lesen), atomares Schreiben, Protokoll mit Wiederholung, Dateiendungen.
Regeln fuer oeffentliche Karten: oeffentlich.py. Laufsperre: laufsperre(mandant) — eine je Mandant fuer wissen und reraise.
Grenze: plugin_api /aktion haelt das Protokoll ueber die ganze Aktion; dauert sie laenger als 60 s, scheitert
prot_mit_wiederholung mit ProtokollGesperrt.
"""
import json
import os
import re
import time

import belegprotokoll as bp

RE_MANDANT = r"[a-z0-9][a-z0-9_-]{0,40}"  # plugin_api.RE_MANDANT ist eine Kopie davon
RE_SAMMLUNG = r"[a-z0-9][a-z0-9_-]{1,40}"
def icloud_platzhalter(name):
    """iCloud Drive: nicht heruntergeladene Datei heisst '.<name>.icloud' (nur ein Platzhalter, nicht der Inhalt)."""
    return name.startswith(".") and name.endswith(".icloud") and len(name) > 8


SF_DATALESS = 0x40000000  # macOS: Datei "dataless" (Inhalt nur in der Wolke, Lesen loest den Download aus)
_stat = os.stat  # Einhaengepunkt fuer Tests (simuliertes st_flags)


def nur_in_wolke(pfad, name=None):
    """True = nur in der Wolke, nicht lesen/hashen (sonst laedt macOS die Datei herunter): alter Platzhaltername '.<name>.icloud'
    oder (ab macOS 14 ueblich) normaler Name mit Merker SF_DATALESS in st_flags."""
    if icloud_platzhalter(name or os.path.basename(pfad)):
        return True
    try:
        return bool(getattr(_stat(pfad), "st_flags", 0) & SF_DATALESS)
    except OSError:
        return False


def wolke_original(pfad):
    """Pfad der Datei, fuer die ein Wolken-Eintrag steht ('.x.pdf.icloud' -> 'x.pdf'; dataless: derselbe Pfad)."""
    d, n = os.path.split(pfad)
    return os.path.join(d, n[1:-7]) if icloud_platzhalter(n) else pfad


ENDUNGEN = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".txt", ".md", ".docx")


def pruefe_namen(mandant, sammlung=None):
    """Die einzige Pruefung fuer Mandant und Sammlung (Pfadteile, QMD-Name). Ungueltig -> ValueError."""
    if not re.fullmatch(RE_MANDANT, mandant or ""):
        raise ValueError("mandant: a-z 0-9 _ - (1-41 Zeichen)")
    if sammlung is not None and not re.fullmatch(RE_SAMMLUNG, sammlung or ""):
        raise ValueError("sammlung: a-z 0-9 _ - (2-41 Zeichen)")


def laufsperre(mandant):
    """Eine Laufsperre je Mandant: <WURZEL>\\<mandant>\\wissen\\.lauf. wissen- und reraise-Laeufe schliessen sich aus, damit
    kein privater Zwilling zwischen Privat-Pruefung und Karte/Upload entsteht. Belegt -> bp.LaufLaeuft."""
    pruefe_namen(mandant)
    return bp.Sperre(os.path.join(bp.WURZEL, mandant, "wissen", ".lauf"), art="lauf")


def kopf_grenzen(md_text):
    """Frontmatter-Text (ohne die ---) und Position des schliessenden '\\n---\\n'; ohne Ende ("", -1)."""
    if not md_text.startswith("---\n"):
        return "", -1
    ende = md_text.find("\n---\n", 3)
    return (md_text[4:ende], ende) if ende >= 0 else ("", -1)


def yaml_wert(v):
    """Ein Wert im Zwilling-Kopf (Gegenstueck: kopf_lesen): ~, true/false, Zahl, Liste oder JSON-Text."""
    if v is None:
        return "~"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(yaml_wert(x) for x in v) + "]"
    return json.dumps(str(v))


def kopf_lesen(md_text):
    """YAML-Kopf, wie yaml_wert ihn schreibt: eine Zeile je Feld, Werte JSON oder ~. Ohne schliessendes --- leer."""
    kopf = {}
    for zeile in kopf_grenzen(md_text)[0].split("\n"):
        k, _, w = zeile.partition(": ")
        if not k:
            continue
        w = w.strip()
        if w == "~":
            kopf[k] = None
            continue
        try:
            kopf[k] = json.loads(w)
        except ValueError:
            kopf[k] = w
    return kopf


def schreibe_atomar(pfad, text):
    """Ganz oder gar nicht: <pfad>.teil schreiben, auf die Platte zwingen, dann ersetzen."""
    d = os.path.dirname(pfad)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(pfad + ".teil", "w", encoding="utf-8", errors="replace", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(pfad + ".teil", pfad)


def prot_mit_wiederholung(pfad, versuche=120, pause=0.5):
    """Protokoll oeffnen (als with-Block); ist es belegt, bis zu versuche x pause (60 s) warten, dann ProtokollGesperrt."""
    for i in range(versuche):
        try:
            return bp.Protokoll(pfad)
        except bp.ProtokollGesperrt:
            if i == versuche - 1:
                raise
            time.sleep(pause)


def selbsttest():
    import tempfile
    for falsch in (("", None), ("Haus", None), ("haus", "x"), ("haus", "a/b"), ("../x", "ab")):
        try:
            pruefe_namen(*falsch)
            raise AssertionError("Name angenommen: %r" % (falsch,))
        except ValueError:
            pass
    pruefe_namen("haus", "belege-2026")
    pruefe_namen("haus")
    assert kopf_lesen('---\ntitel: "x"\n\nkein Ende') == {}
    assert kopf_lesen('---\na: "x"  \nb: ~\nc: 1.5\nd: roh \n---\n') == {"a": "x", "b": None, "c": 1.5, "d": "roh"}
    werte = {"t": 'a "b"\\c\u2028d', "n": None, "j": True, "z": 3, "f": 1.5, "l": [1, "x"]}
    assert kopf_lesen("---\n" + "\n".join("%s: %s" % (k, yaml_wert(w)) for k, w in werte.items()) + "\n---\n") == werte
    with tempfile.TemporaryDirectory() as w:
        p = os.path.join(w, "a", "b.md")
        schreibe_atomar(p, "eins")
        schreibe_atomar(p, "zwei")
        assert open(p, encoding="utf-8").read() == "zwei" and os.listdir(os.path.dirname(p)) == ["b.md"]
        prot = os.path.join(w, "m", "belegprotokoll.jsonl")
        with prot_mit_wiederholung(prot):
            try:
                prot_mit_wiederholung(prot, versuche=2, pause=0.01)
                raise AssertionError("belegtes Protokoll geoeffnet")
            except bp.ProtokollGesperrt:
                pass
        alt, bp.WURZEL = bp.WURZEL, w
        try:
            with laufsperre("m"):
                assert os.path.exists(os.path.join(w, "m", "wissen", ".lauf"))
                try:
                    laufsperre("m").sperren()
                    raise AssertionError("zweite Laufsperre desselben Mandanten erhalten")
                except bp.LaufLaeuft:
                    pass
                with laufsperre("n"):
                    pass  # anderer Mandant: eigene Sperre
        finally:
            bp.WURZEL = alt


if __name__ == "__main__":
    selbsttest()
    print("selbsttest ok")
