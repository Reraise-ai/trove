"""Trove F3 — Messung der Texterkennung an echten Belegen (A-06): Kandidaten gegeneinander, gezaehlt werden
Sekunden je Seite, gefundene Felder und bestandene Verprobung (F4). Ergebnis: CSV + HTML-Bericht in doku\\ und
Zusammenfassung als Notiz im Belegprotokoll. Nichts an den Belegen wird veraendert (nur gelesen).
Aufruf: python messung_f3.py --quelle "<ordner>|20|Belege" --max-seiten 2 --kandidaten rapidocr,docling,granite-vision
"""
import argparse, csv, json, os, statistics, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "plugin", "dashboard"))  # Trove modules live in the plugin package
import belegprotokoll as bp
import lese_treppe as lt
import verprobung as vp

QUELLEN = []  # (Ordner, Anzahl, Gruppe); per --quelle "ordner|anzahl|gruppe" (mehrfach) gesetzt


def stichprobe():
    out = []
    for wurzel, n, gruppe in QUELLEN:
        pdfs = []
        for r, d, f in os.walk(wurzel):
            for fn in sorted(f):
                if fn.lower().endswith(".pdf"):
                    pdfs.append(os.path.join(r, fn))
        pdfs.sort()
        schritt = max(1, len(pdfs) // n)
        out += [(p, gruppe) for p in pdfs[::schritt][:n]]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mandant", default="haus")
    ap.add_argument("--max-seiten", type=int, default=2)
    ap.add_argument("--kandidaten", default="rapidocr,docling,granite-vision")
    ap.add_argument("--quelle", action="append", required=True, help="Ordner|Anzahl|Gruppe (mehrfach)")
    ap.add_argument("--ausgabe", default=os.path.join(bp.WURZEL, "haus", "messung_f3"))
    ap.add_argument("--doku", default="doku")
    a = ap.parse_args()
    for q in a.quelle:
        ordner, n, gruppe = q.rsplit("|", 2)
        QUELLEN.append((ordner, int(n), gruppe))
    os.makedirs(a.ausgabe, exist_ok=True)
    kandidaten = [k for k in a.kandidaten.split(",") if k]
    probe = stichprobe()
    zeilen = []
    log = open(os.path.join(a.ausgabe, "lauf.log"), "a", encoding="utf-8")
    def L(s):
        log.write(time.strftime("%H:%M:%S ") + s + "\n"); log.flush()
    L("Start: %d Belege, Kandidaten %s, max %d Seiten" % (len(probe), kandidaten, a.max_seiten))
    for i, (pfad, gruppe) in enumerate(probe, 1):
        # Treppe normal (E-Rechnung / Textlayer / sonst rapidocr)
        try:
            basis = lt.lesen(pfad, "rapidocr", False, a.max_seiten)
        except Exception as ex:
            L("FEHLER basis %s: %s" % (pfad, ex)); continue
        v = vp.verproben(basis["felder"])
        zeilen.append({"nr": i, "gruppe": gruppe, "datei": os.path.basename(pfad), "seiten": basis["seiten"], "kandidat": "treppe", "methode": basis["methode"],
                       "s_je_seite": round(basis["dauer_s"] / max(1, basis["gelesene_seiten"]), 2), "zeichen": len(basis.get("text", "")),
                       "datum": basis["felder"].get("datum"), "brutto": basis["felder"].get("brutto"), "netto": basis["felder"].get("netto"), "steuer": basis["felder"].get("steuer"),
                       "bestanden": v["bestanden"], "sicher": v["sicher"], "gruende": "; ".join(v["gruende"])})
        tl_brutto = basis["felder"].get("brutto") if basis["methode"] in ("textlayer", "e-rechnung") else None
        for k in kandidaten:
            try:
                erg = lt.lesen(pfad, k, True, a.max_seiten)
            except Exception as ex:
                L("FEHLER %s %s: %s" % (k, pfad, ex))
                zeilen.append({"nr": i, "gruppe": gruppe, "datei": os.path.basename(pfad), "seiten": basis["seiten"], "kandidat": k, "methode": "FEHLER", "gruende": str(ex)[:120]})
                continue
            z = dict(zweite_lesung={"brutto": tl_brutto}) if tl_brutto else {}
            v = vp.verproben(erg["felder"], z.get("zweite_lesung"))
            with open(os.path.join(a.ausgabe, "%s_%02d_%s.json" % (k, i, erg["sha256"][:10])), "w", encoding="utf-8") as f:
                json.dump(erg, f, ensure_ascii=False)
            zeilen.append({"nr": i, "gruppe": gruppe, "datei": os.path.basename(pfad), "seiten": basis["seiten"], "kandidat": k, "methode": erg["methode"],
                           "s_je_seite": erg.get("ocr_s_je_seite"), "zeichen": len(erg.get("text", "")),
                           "datum": erg["felder"].get("datum"), "brutto": erg["felder"].get("brutto"), "netto": erg["felder"].get("netto"), "steuer": erg["felder"].get("steuer"),
                           "bestanden": v["bestanden"], "sicher": v["sicher"], "gruende": "; ".join(v["gruende"]),
                           "brutto_wie_textlayer": (None if tl_brutto is None or erg["felder"].get("brutto") is None else abs(tl_brutto - erg["felder"]["brutto"]) <= 0.02)})
            L("%02d %s %s %s s/Seite=%s bestanden=%s" % (i, k, os.path.basename(pfad)[:40], erg["methode"], erg.get("ocr_s_je_seite"), v["bestanden"]))
        with open(os.path.join(a.ausgabe, "ergebnis.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=sorted({k for z in zeilen for k in z})); w.writeheader(); w.writerows(zeilen)
    # Auswertung
    def q(vals):
        vals = [v for v in vals if isinstance(v, (int, float))]
        return (round(statistics.mean(vals), 2), round(statistics.median(vals), 2)) if vals else (None, None)
    zus = []
    for k in ["treppe"] + kandidaten:
        zs = [z for z in zeilen if z["kandidat"] == k and z.get("methode") != "FEHLER"]
        fehler = sum(1 for z in zeilen if z["kandidat"] == k and z.get("methode") == "FEHLER")
        if not zs:
            zus.append({"kandidat": k, "belege": 0, "fehler": fehler}); continue
        m, med = q([z.get("s_je_seite") for z in zs])
        vergleich = [z["brutto_wie_textlayer"] for z in zs if z.get("brutto_wie_textlayer") is not None]
        zus.append({"kandidat": k, "belege": len(zs), "fehler": fehler, "s_je_seite_mittel": m, "s_je_seite_median": med,
                    "zeichen_mittel": round(statistics.mean([z["zeichen"] for z in zs])),
                    "datum_pct": round(100 * sum(1 for z in zs if z.get("datum")) / len(zs)), "brutto_pct": round(100 * sum(1 for z in zs if z.get("brutto")) / len(zs)),
                    "bestanden_pct": round(100 * sum(1 for z in zs if z["bestanden"]) / len(zs)), "sicher_pct": round(100 * sum(1 for z in zs if z["sicher"]) / len(zs)),
                    "brutto_wie_textlayer_pct": (round(100 * sum(vergleich) / len(vergleich)) if vergleich else None), "verglichen": len(vergleich)})
    with open(os.path.join(a.ausgabe, "zusammenfassung.json"), "w", encoding="utf-8") as f:
        json.dump({"zeit": bp._jetzt(), "belege": len(probe), "max_seiten": a.max_seiten, "kandidaten": zus}, f, ensure_ascii=False, indent=1)
    # HTML-Bericht
    gruppen = sorted({z["gruppe"] for z in zeilen})
    html = ["<!doctype html><html lang='de'><head><meta charset='utf-8'><title>Trove F3 Messung Texterkennung</title><style>body{font-family:Segoe UI,Arial;font-size:10.5pt;margin:28px 34px}h1{font-size:17pt}h2{font-size:12.5pt;border-bottom:1px solid #999;margin-top:16px}table{border-collapse:collapse;width:100%;font-size:9pt}th,td{border:1px solid #bbb;padding:3px 5px;text-align:left;vertical-align:top}th{background:#eee}.k{color:#555;font-size:9.5pt}</style></head><body>",
            "<h1>Trove F3 – Messung der Texterkennung an echten Belegen</h1>",
            "<div class='k'>Messung %s · %d Belege (%s) · je Beleg hoechstens %d Seiten je OCR-Kandidat · Zaehlung: Sekunden je Seite, gefundene Felder (feste Regeln), bestandene Verprobung (F4). Modelle laufen lokal (Ollama, ONNX auf CPU); kein bezahlter Token.</div>" % (bp._jetzt(), len(probe), ", ".join("%d %s" % (sum(1 for p in probe if p[1] == g), g) for g in gruppen), a.max_seiten),
            "<h2>1. Ergebnis je Kandidat</h2><p><b>treppe</b> = die normale Lese-Treppe (E-Rechnung, sonst Textlayer, sonst rapidocr) – das ist der Betriebsweg. Die anderen Zeilen sind erzwungene OCR-Laeufe zum Vergleich, auch bei Belegen mit Textlayer.</p>",
            "<table><tr><th>Kandidat</th><th>Belege</th><th>Fehler</th><th>s/Seite Ø</th><th>s/Seite Median</th><th>Zeichen Ø</th><th>Datum gefunden</th><th>Brutto gefunden</th><th>Verprobung bestanden</th><th>davon sicher (Summen geprueft)</th><th>Brutto = Textlayer</th></tr>"]
    for z in zus:
        html.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s %%</td><td>%s %%</td><td>%s %%</td><td>%s %%</td><td>%s</td></tr>" % (z["kandidat"], z.get("belege"), z.get("fehler"), z.get("s_je_seite_mittel"), z.get("s_je_seite_median"), z.get("zeichen_mittel"), z.get("datum_pct"), z.get("brutto_pct"), z.get("bestanden_pct"), z.get("sicher_pct"), ("%s %% (%d verglichen)" % (z["brutto_wie_textlayer_pct"], z["verglichen"])) if z.get("brutto_wie_textlayer_pct") is not None else "–"))
    html.append("</table><h2>2. Je Beleg</h2><table><tr><th>#</th><th>Gruppe</th><th>Datei</th><th>Seiten</th><th>Kandidat</th><th>Methode</th><th>s/Seite</th><th>Datum</th><th>Brutto</th><th>Netto</th><th>Steuer</th><th>Verprobung</th><th>Gruende</th></tr>")
    for z in zeilen:
        html.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (z["nr"], z["gruppe"], z["datei"][:48], z["seiten"], z["kandidat"], z.get("methode"), z.get("s_je_seite", ""), z.get("datum", ""), z.get("brutto", ""), z.get("netto", ""), z.get("steuer", ""), ("sicher" if z.get("sicher") else "bestanden" if z.get("bestanden") else "Prueflist") if z.get("methode") != "FEHLER" else "FEHLER", (z.get("gruende") or "")[:90]))
    html.append("</table></body></html>")
    os.makedirs(a.doku, exist_ok=True)
    hp = os.path.join(a.doku, "Messung_F3_Texterkennung_2026-10-18.html")
    with open(hp, "w", encoding="utf-8") as f:
        f.write("\n".join(html))
    bp.schreiben(bp.pfad_fuer(a.mandant), ereignis="notiz", werkzeug="messung_f3.py", mandant=a.mandant, lauf="f3-messung", meldung="F3-Messung: %d Belege, Kandidaten %s: %s" % (len(probe), kandidaten, json.dumps(zus, ensure_ascii=False)[:1200]))
    L("Fertig: " + json.dumps(zus, ensure_ascii=False))
    print(json.dumps(zus, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
