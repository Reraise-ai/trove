"""Trove F16 — Regeln fuer oeffentliche Karten (cards with visibility oeffentlich). Nur Standardbibliothek.

Eine oeffentliche Karte enthaelt NICHTS, was aus dem Belegtext oder dem Dateinamen stammt. Positivliste, sonst nichts
(fields such as zugehoerigkeit and sichtbarkeit come from the run settings, not from the document -- see wissen.karte_erzeugen):
  belegdatum   ISO-Datum (geprueft)
  netto/steuer/brutto  Zahlen, NUR wenn verprobung == "sicher" (Summen rechnerisch bestaetigt); sonst keine Betraege
  waehrung     EUR | ARS | USD | GBP | CHF
  verprobung   bestanden | sicher | prueflist
  lesemethode  e-rechnung | textlayer | text | ocr:rapidocr | ocr:docling
  pruefsumme   sha256 (64 hex)
titel  = "Beleg <belegdatum> <brutto> <waehrung>" | ohne Betraege "Beleg <belegdatum> (<verprobung>)" | ohne Datum "Beleg <sha16>"
quelle = "Trove-Beleg sha256 <hex>";  body = genau die Zeilen "<feld>: <wert>";  datum = belegdatum oder None.
pruefe_karte() prueft jede fertige Karte gegen diese Muster (alles andere -> ValueError); kartenfelder() ruft sie immer.
Privat bleibt privat: ein nicht oeffentlicher Zwilling derselben Datei irgendwo (privat_irgendwo) -> keine Karte.
"""
import datetime
import decimal
import glob
import math
import os
import re

import gemeinsam as gm

WAEHRUNGEN = ("EUR", "ARS", "USD", "GBP", "CHF")
VERPROBUNG = ("bestanden", "sicher", "prueflist")
LESEMETHODEN = ("e-rechnung", "textlayer", "text", "ocr:rapidocr", "ocr:docling")
BETRAEGE = ("netto", "steuer", "brutto")
TITEL_MAX = 140  # LIMITS.title in reraise.ai _lib/reraise.js
_ZAHL = r"-?[0-9]+\.[0-9]{2}"  # Dezimaltext mit 2 Nachkommastellen, nie Exponent; [0-9] statt \d: keine Unicode-Ziffern
_DATUM = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
MUSTER = {"belegdatum": _DATUM, "netto": _ZAHL, "steuer": _ZAHL, "brutto": _ZAHL, "waehrung": "|".join(WAEHRUNGEN),
          "verprobung": "|".join(VERPROBUNG), "lesemethode": "|".join(map(re.escape, LESEMETHODEN)), "pruefsumme": r"[0-9a-f]{64}"}
RE_TITEL = re.compile(r"Beleg (?:(?P<sha16>[0-9a-f]{16})|(?P<belegdatum>%s)(?: (?P<brutto>%s)(?: (?P<waehrung>%s))?| \((?P<verprobung>%s)\))?)"
                      % (_DATUM, _ZAHL, MUSTER["waehrung"], MUSTER["verprobung"]))


def _zahl(v):
    """Betrag als Dezimaltext mit 2 Nachkommastellen (0.1+0.2 -> "0.30", 1e16 -> "10000000000000000.00"); sonst None."""
    if not (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)):
        return None
    try:
        return format(decimal.Decimal(str(v)).quantize(decimal.Decimal("0.01")), "f")
    except decimal.InvalidOperation:  # mehr als 28 Stellen
        return None


def iso_datum(d):
    if not (isinstance(d, str) and re.fullmatch(_DATUM, d)):
        return None
    try:
        datetime.date.fromisoformat(d)
    except ValueError:
        return None
    return d


_iso_datum = iso_datum  # alter Name


def titel(f):
    """Titel nur aus den Feldern der Positivliste (f wie in kartenfelder)."""
    if not f.get("belegdatum"):
        return "Beleg " + f["pruefsumme"][:16]
    if f.get("brutto") is not None:
        return ("Beleg %s %s %s" % (f["belegdatum"], f["brutto"], f.get("waehrung") or "")).strip()
    return "Beleg %s (%s)" % (f["belegdatum"], f["verprobung"]) if f.get("verprobung") else "Beleg " + f["belegdatum"]


def pruefe_karte(karte):
    """Typ-/Formatpruefung einer fertigen oeffentlichen Karte {titel, body, quelle, datum} gegen die Positivliste.
    Jeder Wert gegen ein festes Muster; Betraege nur bei verprobung sicher; Titel nur aus Body-Werten. Sonst ValueError."""
    if not isinstance(karte, dict) or set(karte) != {"titel", "body", "quelle", "datum"}:
        raise ValueError("Karte: nur titel, body, quelle, datum erlaubt")
    if not all(isinstance(karte[k], str) for k in ("titel", "body", "quelle")):
        raise ValueError("Karte: titel/body/quelle muessen Text sein")
    f = {}
    for i, zeile in enumerate(karte["body"].split("\n"), 1):
        k, _, w = zeile.partition(": ")
        if k not in MUSTER or k in f or not re.fullmatch(MUSTER[k], w):
            raise ValueError("Karte: body-Zeile %d nicht in der Positivliste" % i)  # Wert nie in die Meldung (kann privat sein)
        f[k] = w
    if "pruefsumme" not in f:
        raise ValueError("Karte: pruefsumme fehlt")
    if "belegdatum" in f and not iso_datum(f["belegdatum"]):
        raise ValueError("Karte: belegdatum kein gueltiges Datum")
    if any(b in f for b in BETRAEGE) and f.get("verprobung") != "sicher":
        raise ValueError("Karte: Betraege nur bei verprobung sicher")
    if karte["quelle"] != "Trove-Beleg sha256 " + f["pruefsumme"]:
        raise ValueError("Karte: quelle nicht in der festen Form")
    if karte["datum"] != f.get("belegdatum"):
        raise ValueError("Karte: datum weicht von belegdatum ab")
    m = RE_TITEL.fullmatch(karte["titel"])
    if not m or len(karte["titel"]) > TITEL_MAX:
        raise ValueError("Karte: titel nicht in der festen Form")
    for k, w in m.groupdict().items():
        if w is not None and w != (f["pruefsumme"][:16] if k == "sha16" else f.get(k)):
            raise ValueError("Karte: titel enthaelt %s, das nicht im body steht" % k)
    return karte


def kartenfelder(sha, kopf):
    """Oeffentliche Karte {titel, body, quelle, datum} nur aus der Positivliste des Zwilling-Kopfs; immer pruefe_karte."""
    if not re.fullmatch(MUSTER["pruefsumme"], sha or ""):
        raise ValueError("pruefsumme ungueltig")
    # Datum nur bei bestandener Verprobung: sonst kann es das erste Datum einer Nicht-Rechnung sein (z. B. Geburtsdatum)
    f = {"belegdatum": iso_datum(kopf.get("belegdatum")) if kopf.get("verprobung") in ("bestanden", "sicher") else None}
    if kopf.get("verprobung") == "sicher":
        f.update({b: _zahl(kopf.get(b)) for b in BETRAEGE})
    for k, liste in (("waehrung", WAEHRUNGEN), ("verprobung", VERPROBUNG), ("lesemethode", LESEMETHODEN)):
        f[k] = kopf.get(k) if isinstance(kopf.get(k), str) and kopf.get(k) in liste else None
    f["pruefsumme"] = sha
    f = {k: w for k, w in f.items() if w is not None}
    return pruefe_karte({"titel": titel(f), "body": "\n".join("%s: %s" % kw for kw in f.items()),
                         "quelle": "Trove-Beleg sha256 " + sha, "datum": f.get("belegdatum")})


def zwillings_index(wissen):
    """Einmal je Lauf (unter der Laufsperre): sha16 -> [md] ueber alle Sammlungen unter wissen (wie das glob in zwillinge).
    Ohne Index waere die Zwillingssuche je Datei ein Verzeichnislauf (5000 Zwillinge ~40 s). Neue Zwillinge: index_eintragen."""
    index = {}
    try:
        sammlungen = [e.path for e in os.scandir(wissen) if e.is_dir() and not e.name.startswith(".")]
    except FileNotFoundError:
        return index
    for d in sammlungen:
        for e in os.scandir(d):
            if len(e.name) > 19 and e.name[16] == "_" and e.name.lower().endswith(".md") and e.is_file():
                index.setdefault(e.name[:16].lower(), []).append(e.path)
    for liste in index.values():
        liste.sort()
    return index


def index_eintragen(index, md):
    if index is not None:
        liste = index.setdefault(os.path.basename(md)[:16].lower(), [])
        if md not in liste:
            liste.append(md)
            liste.sort()


def zwillinge(wissen, sha, index=None):
    """Alle Markdown-Zwillinge dieser Datei in allen Sammlungen unter wissen: [(sammlung, md, kopf, text)]. index: zwillings_index."""
    out = []
    if index is None:
        mds = sorted(glob.glob(os.path.join(glob.escape(wissen), "*", sha[:16] + "_*.md")))
    else:
        mds = list(index.get(sha[:16], ()))
    for md in mds:
        with open(md, encoding="utf-8", errors="replace") as f:
            text = f.read()
        out.append((os.path.basename(os.path.dirname(md)), md, gm.kopf_lesen(text), text))
    return out


def privat_irgendwo(wissen, sha, index=None):
    """Sammlungen, in denen ein Zwilling derselben Datei nicht oeffentlich ist. Ausgenommen nur ein Zwilling, dessen pruefsumme
    ein gueltiger, anderer sha256 ist (andere Datei, gleicher sha16); fehlt/leer/kaputt/Zahl -> vorsichtshalber privat.
    Je Mandant; andere Mandanten werden bewusst nicht angesehen."""
    return sorted({s for s, _, kopf, _ in zwillinge(wissen, sha, index)
                   if not _fremde_summe(kopf.get("pruefsumme"), sha) and kopf.get("sichtbarkeit") != "oeffentlich"})


def _fremde_summe(p, sha):
    return isinstance(p, str) and re.fullmatch(MUSTER["pruefsumme"], p) is not None and p != sha


def selbsttest():
    import json
    import tempfile
    sha = "a" * 64
    geheim = {"titel": "DNI 12.345.678 Juan Perez.pdf", "quelle": "C:\\Beispiel\\ablage\\r.pdf",
              "aussteller_kennung": "20-12345678-9", "rechnungsnummer": "0003-00012345", "iban": "DE89 3704 0044 0532 0130 00",
              "traeger": "Person", "zugehoerigkeit": "Muster privat", "notiz": "Tel +00 0 000-0000, muster@example.com"}
    beleg = {**geheim, "belegdatum": "2026-03-01", "brutto": 1210.5, "netto": 1000, "steuer": 210.5, "waehrung": "ARS",
             "lesemethode": "ocr:rapidocr"}
    nie = ("12345678", "12.345.678", "Juan", "Perez", "geheim", "Users", "r.pdf", "0003", "20-123", "3704", "4567", "@",
           "Person", "privat", "Muster")
    for verprobung in ("bestanden", "prueflist", "sicher", None):
        k = kartenfelder(sha, {**beleg, "verprobung": verprobung})
        alles = json.dumps(k, ensure_ascii=False)
        for s in nie:
            assert s not in alles, (s, alles)
        assert set(k) == {"titel", "body", "quelle", "datum"} and k["quelle"] == "Trove-Beleg sha256 " + sha, k
        betraege = [z for z in k["body"].split("\n") if z.split(":")[0] in BETRAEGE]
        assert bool(betraege) == (verprobung == "sicher"), (verprobung, k)  # Betraege nur bei sicher
    assert k["titel"] == "Beleg " + sha[:16] and k["datum"] is None and "2026" not in json.dumps(k), k  # ohne Verprobung kein Datum
    k = kartenfelder(sha, {**beleg, "verprobung": "bestanden"})
    assert k["titel"] == "Beleg 2026-03-01 (bestanden)" and k["datum"] == "2026-03-01", k
    k = kartenfelder(sha, {**beleg, "verprobung": "sicher"})
    assert k["titel"] == "Beleg 2026-03-01 1210.50 ARS", k
    assert [z.split(":")[0] for z in k["body"].split("\n")] == ["belegdatum", "netto", "steuer", "brutto", "waehrung",
                                                                "verprobung", "lesemethode", "pruefsumme"], k["body"]
    k = kartenfelder(sha, {**beleg, "verprobung": "prueflist"})
    assert k["titel"] == "Beleg " + sha[:16] and "1210" not in json.dumps(k) and "2026" not in json.dumps(k), k  # E-C3
    # Fremdwerte in erlaubten Feldern fallen weg
    k = kartenfelder(sha, {"brutto": "12345678", "waehrung": "ARS pesos", "verprobung": "sicher", "lesemethode": "ocr:juan",
                           "belegdatum": "2026-02-30", "netto": True, "steuer": float("nan")})
    assert k == {"titel": "Beleg " + "a" * 16, "body": "verprobung: sicher\npruefsumme: " + sha,
                 "quelle": "Trove-Beleg sha256 " + sha, "datum": None}, k
    # pruefe_karte faengt alles ausserhalb der Positivliste
    gut = kartenfelder(sha, {**beleg, "verprobung": "sicher"})
    for x, text in ((0.1 + 0.2, "0.30"), (1e16, "10000000000000000.00"), (1210, "1210.00"), (1e40, None)):
        assert _zahl(x) == text, (x, _zahl(x))
    for falsch in ("1e+16", "0.30000000000000004", "1210.5", "1210", "1.234"):
        try:
            pruefe_karte({**gut, "body": gut["body"].replace("brutto: 1210.50", "brutto: " + falsch),
                          "titel": gut["titel"].replace("1210.50", falsch)})
            raise AssertionError("Betragsformat angenommen: %s" % falsch)
        except ValueError:
            pass
    for kaputt in ({**gut, "x": 1}, {**gut, "body": gut["body"] + "\nrechnungsnummer: 0003-00012345"},
                   {**gut, "body": gut["body"].replace("verprobung: sicher", "verprobung: prueflist")},
                   {**gut, "titel": "Beleg r.pdf"}, {**gut, "titel": "Beleg 2026-03-01 12345678 ARS"},
                   {**gut, "quelle": "Trove-Beleg r.pdf, sha256 " + sha}, {**gut, "datum": "2026-03-02"},
                   {**gut, "body": gut["body"].replace("2026-03-01", "2026-02-30")}, {**gut, "body": gut["body"] + "\npruefsumme: " + sha},
                   {**gut, "body": gut["body"].replace("pruefsumme: ", "pruefsumme: x")}, {**gut, "titel": None},
                   {**gut, "body": gut["body"].replace("brutto: 1210.50", "brutto: \u0661\u0662\u0661\u0660.50"),
                    "titel": gut["titel"].replace("1210.50", "\u0661\u0662\u0661\u0660.50")},  # Unicode-Ziffern (U4)
                   {**gut, "body": gut["body"].replace("2026-03-01", "\uff12\uff10\uff12\uff16-03-01"),
                    "titel": gut["titel"].replace("2026-03-01", "\uff12\uff10\uff12\uff16-03-01"),
                    "datum": "\uff12\uff10\uff12\uff16-03-01"},
                   {**gut, "titel": "Beleg 2026-03-01 %s.00 ARS" % ("1" * 137),
                    "body": gut["body"].replace("brutto: 1210.50", "brutto: " + "1" * 137 + ".00")}):  # Titel ueber TITEL_MAX
        try:
            pruefe_karte(kaputt)
            raise AssertionError("pruefe_karte angenommen: %r" % kaputt)
        except ValueError:
            pass
    # Titel nur aus Body-Werten: Datum/Betrag im Titel, die der prueflist-Body nicht traegt -> abgelehnt (M06)
    pl = kartenfelder(sha, {**beleg, "verprobung": "prueflist"})
    for t in ("Beleg 2026-03-01 1210.50 ARS", "Beleg " + "b" * 16):
        try:
            pruefe_karte({**pl, "titel": t})
            raise AssertionError("Titel ohne Body-Deckung angenommen: %s" % t)
        except ValueError:
            pass
    # kartenfelder ohne pruefe_karte faellt auf: ein undichter Titel muss scheitern
    g = globals()
    alt = g["titel"]
    g["titel"] = lambda f: "Beleg " + geheim["titel"]
    try:
        kartenfelder(sha, beleg)
        raise AssertionError("kartenfelder ohne pruefe_karte")
    except ValueError:
        pass
    finally:
        g["titel"] = alt
    with tempfile.TemporaryDirectory() as w:
        sha = "b" * 64
        for s, kopf in (("oeff", '---\npruefsumme: "%s"\nsichtbarkeit: "oeffentlich"\n---\n' % sha),
                        ("priv", '---\npruefsumme: "%s"\nsichtbarkeit: "privat"\n---\n' % sha),
                        ("fremd", '---\npruefsumme: "%s"\nsichtbarkeit: "privat"\n---\n' % ("c" * 64)),
                        ("ohnesumme", '---\nsichtbarkeit: "privat"\n---\n'),
                        ("ohnesicht", '---\npruefsumme: "%s"\n---\n' % sha),
                        ("kaputt", '---\npruefsumme: "%s"\nsichtbarkeit: "oeffentlich"\n' % sha),
                        ("leer", '---\npruefsumme: ""\nsichtbarkeit: "privat"\n---\n'),
                        ("gross", '---\npruefsumme: "%s"\nsichtbarkeit: "privat"\n---\n' % sha.upper()),
                        ("zahl", '---\npruefsumme: 12345\nsichtbarkeit: "privat"\n---\n'),
                        ("kurz", '---\npruefsumme: "%s"\nsichtbarkeit: "privat"\n---\n' % ("c" * 63))):
            gm.schreibe_atomar(os.path.join(w, s, sha[:16] + "_r.md"), kopf)
        assert privat_irgendwo(w, sha) == ["gross", "kaputt", "kurz", "leer", "ohnesicht", "ohnesumme", "priv", "zahl"] \
            and len(zwillinge(w, sha)) == 10  # "fremd" (gueltiger anderer sha256) zaehlt nicht (U5)
        gm.schreibe_atomar(os.path.join(w, ".versteckt", sha[:16] + "_r.md"), "x")  # glob "*" sieht sie nicht, der Index auch nicht
        gm.schreibe_atomar(os.path.join(w, "priv", sha[:16] + "_r.md.teil"), "x")
        index = zwillings_index(w)
        assert zwillinge(w, sha, index) == zwillinge(w, sha) and privat_irgendwo(w, sha, index) == privat_irgendwo(w, sha)
        neu = os.path.join(w, "spaet", sha[:16] + "_r.md")
        gm.schreibe_atomar(neu, '---\npruefsumme: "%s"\nsichtbarkeit: "privat"\n---\n' % sha)
        assert "spaet" not in privat_irgendwo(w, sha, index)
        index_eintragen(index, neu)
        assert zwillinge(w, sha, index) == zwillinge(w, sha) and "spaet" in privat_irgendwo(w, sha, index)
        os.remove(neu)
        for s in ("priv", "ohnesumme", "ohnesicht", "kaputt", "leer", "gross", "zahl", "kurz"):
            os.remove(os.path.join(w, s, sha[:16] + "_r.md"))
        assert privat_irgendwo(w, sha) == []


if __name__ == "__main__":
    selbsttest()
    print("selbsttest ok")
