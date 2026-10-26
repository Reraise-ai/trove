"""Trove Dauerlauf: ueberwachte Ordner (trove.json "ueberwacht", eingetragen durch "Ins Wissen") still nachfuehren.
Je Lauf und Ordner, inkrementell: unveraenderte Datei (Groesse + mtime gleich) wird nicht gelesen und nicht gehasht;
neue Datei -> Zwilling (+ Karte); geaenderte Datei (sha anders) -> neuer Zwilling, der alte "ersetzt"; verschwundene Datei ->
Zwilling und Karte "nicht-mehr-vorhanden" (Kopfzeile trove_zustand, nichts wird geloescht). Danach qmd update + embed.
Die Quelle (Kundenablage) wird nur gelesen. Mandanten-Laufsperre verhindert Ueberlappung. Stand: <daten>/<mandant>/dauerlauf/<sammlung>.json.
Aufruf: python dauerlauf.py [--ohne-embed] [--json]   (Planer: installieren.py --dauerlauf 30)
Fehler sichtbar: je Ordner wird jede Ausnahme gefangen (die anderen Ordner laufen weiter), je Ordner eine Logzeile mit Zeitstempel
(inkl. qmd-Fehler), Marker <datenwurzel>/dauerlauf_letzter_lauf.json {zeit, ordner, neu, geaendert, weg, fehler:[...]} (liest der
Gesundheitscheck), Rueckgabe 1 bei Fehlern. Scheitert qmd, bleibt <sammlung>.json.qmd_offen liegen -> naechster Lauf wiederholt qmd.
Nicht lesbarer Ordner/Teilbaum (macOS-Datenschutz, Rechte): nichts darunter gilt als weg, sichtbare Meldung "Ordner nicht lesbar: ...".
Kehrt eine Datei mit gleichem Inhalt zurueck, wird die Markierung an Zwilling und Karte zurueckgenommen."""
import argparse, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import belegprotokoll as bp
import gemeinsam as gm
import konfig
import oeffentlich as oe
import wissen as w
import wissenskarte as wk


def _stand_pfad(mandant, sammlung):
    return os.path.join(bp.WURZEL, mandant, "dauerlauf", sammlung + ".json")


def _stand_lesen(p):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def _markieren(l, sha, zustand):
    """Zwilling (und seine Wiki-Karte) einer sha als ersetzt / nicht-mehr-vorhanden kennzeichnen; zustand None nimmt die Markierung
    zurueck. Liefert Zahl geaenderter Dateien."""
    n = 0
    for md in l.index.get(sha[:16].lower(), ()):
        if os.path.dirname(md) != l.ordner:
            continue
        n += wk.kopf_setzen(md, "trove_zustand", zustand)
        kopf, _ = w.zwilling_lesen(md)
        karte = str(kopf.get("karte") or "")
        if karte.startswith("wiki:"):
            cp = os.path.join(wk.wiki_wurzel(), *karte[5:].split("/")) + ".md"
            if os.path.exists(cp) and wk.kopf_setzen(cp, "trove_zustand", zustand):
                wk.GEAENDERT.append(cp)
                n += 1
    return n


def nachfuehren(e, embed=True, mandant=None, log=True):
    """log=False: die Zeile in wiki/log.md schreibt der Aufrufer (main: eine Zeile fuer den ganzen Lauf ueber alle Ordner)."""
    mandant = mandant or konfig.mandant()
    if log:
        wk.GEAENDERT.clear()
    zug = (e.get("zugehoerigkeit") or "").strip() or w.ZUG_OFFEN
    sicht = e.get("sichtbarkeit") or "privat"
    karten = True  # one Trove: a knowledge card per file (the wiki is the knowledge target)
    l = w.Lauf(mandant=mandant, sammlung=e["sammlung"], sichtbarkeit=sicht, zugehoerigkeit=zug, wurzel=e["ordner"],
               traeger=e.get("traeger") or w.traeger_auto(zug), karten=karten, embed=embed)
    z = {"ordner": e["ordner"], "sammlung": e["sammlung"], "neu": [], "uebernommen": 0, "geaendert": [], "weg": [], "umbenannt": [], "unveraendert": 0,
         "nur_in_wolke": 0, "fehler": []}
    if not os.path.isdir(e["ordner"]):
        z["fehler"].append("Folder not reachable (nothing marked as gone): " + e["ordner"])
        return z
    sp = _stand_pfad(mandant, e["sammlung"])
    with gm.laufsperre(mandant):
        os.makedirs(l.ordner, exist_ok=True)
        l.index = oe.zwillings_index(os.path.dirname(l.ordner))
        stand = _stand_lesen(sp)
        unlesbar, z["hinweise"], ng = [], [], []
        dateien, _, wolke = w.dateien_sammeln(mandant, e["ordner"], unlesbar, z["hinweise"], ng)
        if ng:
            z["hinweise"].append(w.nicht_gelesen_hinweis(ng))
        z["nur_in_wolke"] = len(wolke)
        z["fehler"] += [w.unlesbar_meldung(u) for u in unlesbar]
        jetzt, ausser = set(dateien) | {gm.wolke_original(p) for p in wolke}, set()  # ausgelagert (iCloud) = vorhanden, nicht weg
        for p in dateien:
            try:
                st = os.stat(p)
                alt = stand.get(p) if stand.get(p) and not stand[p].get("weg") else None
                if alt and alt["groesse"] == st.st_size and alt["mtime_ns"] == st.st_mtime_ns:
                    z["unveraendert"] += 1
                    continue
                sha = bp.datei_sha(p)
                if alt and alt["sha"] == sha:  # nur angefasst
                    alt.update(groesse=st.st_size, mtime_ns=st.st_mtime_ns)
                    z["unveraendert"] += 1
                    continue
                r = w.markdown_erzeugen(l, p)
                if karten:
                    w.karte_erzeugen(l, r)
                if alt:
                    ausser.add(alt["sha"])
                    z["geaendert"].append(p)
                elif r["zustand"] == "neu":
                    z["neu"].append(p)
                else:
                    z["uebernommen"] += 1
                if r["zustand"] == "schon_da" and w.quelle_umziehen(l, r["md"], p):  # umbenannt/verschoben: Karte nennt neuen Ort
                    z["umbenannt"].append(p)
                if r["zustand"] == "schon_da" and _markieren(l, sha, None):  # Inhalt wieder da: falsche Markierung zuruecknehmen
                    z.setdefault("zurueckgenommen", []).append(p)
                stand[p] = {"groesse": st.st_size, "mtime_ns": st.st_mtime_ns, "sha": sha, "md": r["md"], "weg": False}
            except (bp.ProtokollGesperrt, bp.ProtokollDefekt):
                raise
            except Exception as ex:
                z["fehler"].append("%s: %s" % (p, str(ex)[:200]))
        da = {v["sha"] for q, v in stand.items() if q in jetzt and not v.get("weg")}
        for p, alt in list(stand.items()):
            if p not in jetzt and not alt.get("weg") and os.path.isdir(e["ordner"]) and not w.unter_einem(p, unlesbar):
                if alt["sha"] in da:  # Inhalt liegt jetzt anderswo (umbenannt/verschoben/Dublette): Eintrag faellt weg, nicht "weg" zaehlen
                    q = next(q for q, v in stand.items() if q in jetzt and not v.get("weg") and v["sha"] == alt["sha"])
                    if alt.get("md") and os.path.exists(alt["md"]) and w.quelle_umziehen(l, alt["md"], q) and q not in z["umbenannt"]:
                        z["umbenannt"].append(q)
                    del stand[p]
                    continue
                alt["weg"] = True
                ausser.add(alt["sha"])
                z["weg"].append(p)
        lebend = {v["sha"] for p, v in stand.items() if not v.get("weg") and p in jetzt}
        for sha in ausser - lebend:
            weg = any(stand[p]["sha"] == sha for p in z["weg"] if p in stand)
            _markieren(l, sha, "nicht-mehr-vorhanden" if weg else "ersetzt")
        aenderungen = len(z["neu"]) + len(z["geaendert"]) + len(z["weg"]) + z["uebernommen"] + len(z["umbenannt"])
        qmd_offen = sp + ".qmd_offen"
        if aenderungen or os.path.exists(qmd_offen):
            try:
                z["qmd"] = w.qmd_sammlung(l)
            except Exception as ex:  # qmd fehlt / startet nicht: Zwillinge sind geschrieben, Stand wird trotzdem gespeichert
                z["qmd"] = {"fehler": 1, "meldung": "%s: %s" % (type(ex).__name__, str(ex)[:300])}
            if z["qmd"].get("fehler"):
                z["fehler"].append("qmd: " + qmd_text(z["qmd"]))
                os.makedirs(os.path.dirname(qmd_offen), exist_ok=True)
                gm.schreibe_atomar(qmd_offen, bp.jetzt())
            elif os.path.exists(qmd_offen):
                os.remove(qmd_offen)
        if aenderungen:
            w._notiz(l, werkzeug="dauerlauf.py", meldung="Continuous run %s: %d new, %d changed, %d gone, %d unchanged, %d errors" % (
                e["ordner"], len(z["neu"]), len(z["geaendert"]), len(z["weg"]), z["unveraendert"], len(z["fehler"])))
        gm.schreibe_atomar(sp, json.dumps(stand, ensure_ascii=False, indent=1))
    if log:
        z["log"] = wk.log_lauf()
    return z


def qmd_text(q):
    """Kurztext der qmd-Fehler eines Laufs (Meldung, update/embed/angelegt mit rc != 0)."""
    teile = [q["meldung"]] if q.get("meldung") else []
    for k in ("angelegt", "update", "embed"):
        v = q.get(k)
        if isinstance(v, (list, tuple)) and len(v) == 2 and v[0] != 0:
            teile.append("%s rc %s: %s" % (k, v[0], str(v[1])[-200:].replace("\n", " ")))
    if isinstance(q.get("index_yml"), str) and q.get("update") == "not run (index.yml)":
        teile.append("index.yml " + q["index_yml"])
    return "; ".join(teile) or "Fehler (ohne Text)"


def marker_pfad():
    return os.path.join(konfig.datenwurzel(), "dauerlauf_letzter_lauf.json")


def lauf(liste, embed=True):
    """Alle ueberwachten Ordner nachfuehren; jede Ausnahme je Ordner gefangen. Schreibt den Marker. Liefert (Ergebnisse, Marker)."""
    wk.GEAENDERT.clear()
    out = []
    for e in liste:
        try:
            out.append(nachfuehren(e, embed=embed, log=False))
        except bp.LaufLaeuft:
            out.append({"ordner": e.get("ordner"), "uebersprungen": "a run is already active"})
        except Exception as ex:  # auch ProtokollGesperrt/-Defekt, fehlender Wiki-Ordner, volle Platte: dieser Ordner Fehler, die anderen laufen
            out.append({"ordner": e.get("ordner"), "neu": [], "geaendert": [], "weg": [], "unveraendert": 0,
                        "fehler": ["%s: %s" % (type(ex).__name__, str(ex)[:300])]})
    allg = []
    try:
        wk.log_lauf()  # EINE Zeile in wiki/log.md fuer den ganzen Lauf
    except Exception as ex:
        allg.append("wiki/log.md: %s: %s" % (type(ex).__name__, str(ex)[:200]))
    marker = {"zeit": bp.jetzt(), "ordner": [z.get("ordner") for z in out],
              "neu": sum(len(z.get("neu") or []) for z in out), "geaendert": sum(len(z.get("geaendert") or []) for z in out),
              "weg": sum(len(z.get("weg") or []) for z in out),
              "fehler": ["%s: %s" % (z.get("ordner"), f) for z in out for f in z.get("fehler") or []] + allg,
              "uebersprungen": ["%s: %s" % (z.get("ordner"), z["uebersprungen"]) for z in out if z.get("uebersprungen")],
              "hinweise": [h for z in out for h in z.get("hinweise") or []]}
    try:
        gm.schreibe_atomar(marker_pfad(), json.dumps(marker, ensure_ascii=False, indent=1))
    except OSError as ex:
        marker["fehler"].append("Marker not written: %s" % ex)
    return out, marker


def logzeilen(out, marker):
    t = time.strftime("%Y-%m-%d %H:%M:%S")
    if not out:
        return ["%s | nothing to do (trove.json ueberwacht is empty)" % t] + ["%s | ERROR %s" % (t, f) for f in marker["fehler"]]
    zeilen = []
    for z in out:
        if z.get("uebersprungen"):
            zeilen.append("%s | %s: uebersprungen (%s)" % (t, z.get("ordner"), z["uebersprungen"]))
            continue
        zeilen.append("%s | %s: %d neu, %d geaendert, %d weg, %d unveraendert, %d Fehler%s" % (
            t, z.get("ordner"), len(z["neu"]), len(z["geaendert"]), len(z["weg"]), z["unveraendert"], len(z["fehler"]),
            "".join(" | FEHLER " + f for f in z["fehler"]) + "".join(" | HINWEIS " + h for h in z.get("hinweise") or [])))
    return zeilen + ["%s | ERROR %s" % (t, f) for f in marker["fehler"] if not any(f.startswith(str(z.get("ordner")) + ": ") for z in out)]


def main():
    ap = argparse.ArgumentParser(description="Trove Dauerlauf")
    ap.add_argument("--ohne-embed", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    os.environ.setdefault("TROVE_AGENT", "trove:dauerlauf")
    try:
        liste = konfig.lesen().get("ueberwacht", [])
    except Exception as ex:
        liste, kaputt = [], "trove.json not readable: %s" % ex
    else:
        kaputt = None
    out, marker = lauf(liste, embed=not a.ohne_embed)
    if kaputt:
        marker["fehler"].append(kaputt)
        gm.schreibe_atomar(marker_pfad(), json.dumps(marker, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False) if a.json else "\n".join(logzeilen(out, marker)), flush=True)
    return 1 if marker["fehler"] else 0


if __name__ == "__main__":
    sys.exit(main())
