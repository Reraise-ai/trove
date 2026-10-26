"""Trove: knowledge card for a wiki (raw/ = sources, wiki/ = maintained pages).
The text twin (derived, not a copy of the original; its head carries the original path + sha256) lives under <wiki>/../raw/trove/<collection>/;
the card is wiki/quellen/trove-<sha16>-<slug>.md with page head typ/quellen/stand/status (status: zu-pruefen, "to review", with umlaut).
wiki/log.md: ONE line per run (log_lauf), "YYYY-MM-DD HH:MM | Trove | <n> Karten / Seiten | Widersprüche: 0", before the closing ``` of the
code block; appended if there is no code block. Never a double card: file exists = card existed.
Setting: wiki_wurzel in trove.json / TROVE_WIKI (default <data root>/knowledge/wiki, created on demand; a custom path must exist).
Author (new cards only): head lines created_by (which agent wrote the card: TROVE_AGENT, default "trove") and source (original file).
A Trove card is never a statement or decision of the human.
"""
import os
import re
import time
import unicodedata

import gemeinsam as gm
import konfig

GEAENDERT = []  # Wiki-Seiten, die dieser Lauf neu geschrieben oder markiert hat (log_lauf schreibt daraus eine Zeile)


def wiki_wurzel(anlegen=True):
    """The knowledge target. Default: <data root>/knowledge/wiki (created on demand). A path set by the user must already exist."""
    eigen = os.environ.get("TROVE_WIKI")
    w = os.path.expanduser(eigen) if eigen else os.path.join(konfig.datenwurzel(), "knowledge", "wiki")
    if not os.path.isdir(w):
        if eigen or not anlegen:
            if eigen:
                raise ValueError("Wiki folder not found: %s (setting wiki_wurzel in trove.json / TROVE_WIKI)" % w)
            return w
        os.makedirs(w, exist_ok=True)
    return w


def _slug(s, n=50):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()[:n] or "beleg"


def raw_ziel(sammlung):
    return os.path.join(os.path.dirname(wiki_wurzel()), "raw", "trove", sammlung)


def raw_rel(sammlung, md):
    return "raw/trove/%s/%s" % (sammlung, os.path.basename(md))


def urheber():
    return (os.environ.get("TROVE_AGENT") or "trove").strip() or "trove"


def karte_schreiben(sammlung, md, sha, titel, inhalt, quelle=None, zugehoerigkeit=None):
    """Schreibt die Wissenskarte. Liefert (kartenkennung, neu). Kennung "wiki:quellen/<datei ohne .md>" -> Kopfzeile karte: im Zwilling.
    Herkunft: source (Originaldatei) und ordner (Ordner in der Ablage des Kunden, damit seine Ordnerlogik sichtbar bleibt);
    zugehoerigkeit "offen", wenn nichts angegeben wurde."""
    wiki = wiki_wurzel()
    rel = raw_rel(sammlung, md)  # der Zwilling liegt im Wiki-Modus von vornherein dort (Lauf.ordner), keine Kopie
    name = "trove-%s-%s" % (sha[:16], _slug(os.path.splitext(os.path.basename(md))[0].split("_", 1)[-1]))
    pfad = os.path.join(wiki, "quellen", name + ".md")
    kennung = "wiki:quellen/" + name
    if os.path.exists(pfad):
        return kennung, False
    heute = time.strftime("%Y-%m-%d")
    src = " ".join(str(quelle or rel).split())
    ordner = " ".join(os.path.dirname(str(quelle)).split()) if quelle else ""
    zug = " ".join(str(zugehoerigkeit or "offen").split())
    seite = ("---\ntyp: quelle\nquellen: [%s]\nstand: %s\nstatus: zu-prüfen\ncreated_by: %s\nsource: %s\n%szugehoerigkeit: %s\n---\n\n# %s\n\n%s\n\n"
             "%sZwilling: `%s` (Prüfsumme sha256 `%s`)\n" % (rel, heute, urheber(), src, ("ordner: %s\n" % ordner) if ordner else "", zug, titel,
                                                              inhalt.strip(), ("Ordner bei dir: `%s`  \n" % ordner) if ordner else "", rel, sha))
    os.makedirs(os.path.dirname(pfad), exist_ok=True)
    gm.schreibe_atomar(pfad, seite)
    GEAENDERT.append(pfad)
    return kennung, True


def kopf_setzen(pfad, key, wert):
    """Eine Kopfzeile "key: wert" in einer Markdown-Datei setzen (ersetzen oder vor dem schliessenden --- einfuegen); wert None = Zeile
    entfernen. True bei Aenderung."""
    with open(pfad, encoding="utf-8") as fh:
        text = fh.read()
    roh, ende = gm.kopf_grenzen(text)
    if ende < 0:
        raise ValueError("Datei ohne Kopfdaten: " + pfad)
    zeilen = roh.split("\n")
    neu = "%s: %s" % (key, wert)
    i = next((k for k, z in enumerate(zeilen) if z.startswith(key + ":")), None)
    if wert is None:
        if i is None:
            return False
        del zeilen[i]
    elif i is not None and zeilen[i] == neu:
        return False
    elif i is None:
        zeilen.append(neu)
    else:
        zeilen[i] = neu
    gm.schreibe_atomar(pfad, "---\n" + "\n".join(zeilen) + text[ende:])
    return True


def log_einfuegen(alt, zeile):
    """Zeile in den Text von log.md: vor den schliessenden ``` des letzten Codeblocks; ohne (geschlossenen) Codeblock ans Ende."""
    zeilen = alt.split("\n")
    zaun = [i for i, z in enumerate(zeilen) if z.strip().startswith("```")]
    if len(zaun) >= 2 and len(zaun) % 2 == 0:
        i = zaun[-1]
        return "\n".join(zeilen[:i] + [zeile] + zeilen[i:])
    return alt + ("" if not alt or alt.endswith("\n") else "\n") + zeile + "\n"


def log_lauf(widersprueche=0):
    """EINE Zeile je Lauf in wiki/log.md, nur wenn der Lauf Wiki-Seiten geschrieben/markiert hat. Liefert die Zeile oder None."""
    n = len(set(GEAENDERT))
    GEAENDERT.clear()
    if not n:
        return None
    zeile = "%s | Trove | %d Karten / Seiten | Widersprüche: %d" % (time.strftime("%Y-%m-%d %H:%M"), n, widersprueche)
    logp = os.path.join(wiki_wurzel(), "log.md")
    alt = open(logp, encoding="utf-8").read() if os.path.exists(logp) else ""
    gm.schreibe_atomar(logp, log_einfuegen(alt, zeile))
    return zeile


def selbsttest():
    import tempfile
    blk = "# Log\n\n```\n2026-10-01 09:00 | Einrichtung | index.md | Widersprüche: 0\n```\n\nNachsatz\n"
    neu = log_einfuegen(blk, "Z")
    assert neu == "# Log\n\n```\n2026-10-01 09:00 | Einrichtung | index.md | Widersprüche: 0\nZ\n```\n\nNachsatz\n", repr(neu)
    assert log_einfuegen("", "Z") == "Z\n" and log_einfuegen("a", "Z") == "a\nZ\n"
    assert log_einfuegen("```\noffen\n", "Z") == "```\noffen\nZ\n"  # offener Block: anhaengen
    with tempfile.TemporaryDirectory() as t:
        os.environ["TROVE_WIKI"] = t
        open(os.path.join(t, "log.md"), "w", encoding="utf-8").write(blk)
        assert log_lauf() is None  # nichts geaendert: keine Zeile
        GEAENDERT.extend(["a", "b", "a"])
        z = log_lauf()
        assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d \| Trove \| 2 Karten / Seiten \| Widersprüche: 0", z), z
        text = open(os.path.join(t, "log.md"), encoding="utf-8").read()
        assert text.count(z) == 1 and text.index(z) < text.rindex("```") and text.endswith("```\n\nNachsatz\n"), text
        assert not GEAENDERT


if __name__ == "__main__":
    selbsttest()
    print("selbsttest ok")
