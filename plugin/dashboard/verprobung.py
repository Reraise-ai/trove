"""Trove F4 — Verprobung: feste Regeln pruefen das Ausgelesene; nur Bestandenes gilt als sicher, der Rest geht in die Prueflist.
Keine KI. Regeln:
  R1 Datum vorhanden und plausibel (2015 .. heute+1 Jahr)
  R2 Bruttobetrag vorhanden und > 0
  R3 Summen: netto + steuer = brutto (±0,02) — oder steuer = netto × Satz (AR 21 / 10,5 / 27 %, DE 19 / 7 %, ±0,05)
  R4 CUIT-Pruefziffer (Argentinien, mod 11) — nur wenn CUIT gefunden
  R5 IBAN-Pruefziffer (mod 97) — nur wenn IBAN gefunden
  R6 USt-IdNr Form (DE + 9 Ziffern) — nur wenn gefunden
  R7 zwei Auslesungen stimmen beim Brutto ueberein — nur wenn zwei Lesungen vorliegen
Bestanden = R1 und R2 erfuellt, R3 erfuellt oder nicht pruefbar, keine Pruefziffer falsch, R7 nicht widerspruechlich.
Jedes Ergebnis: Zeile in <TROVE_DATEN>\\<mandant>\\pruefliste.jsonl (append) und Ereignis "verprobt" im Belegprotokoll.
"""
import argparse, datetime, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import belegprotokoll as bp

SAETZE = (0.21, 0.105, 0.27, 0.19, 0.07, 0.0)


def cuit_ok(cuit):
    z = [int(c) for c in cuit.replace("-", "") if c.isdigit()]
    if len(z) != 11:
        return False
    gew = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    r = sum(a * b for a, b in zip(z[:10], gew)) % 11
    pz = 11 - r
    if pz == 11:
        pz = 0
    elif pz == 10:
        pz = 9
    return pz == z[10]


def iban_ok(iban):
    s = "".join(c for c in iban.upper() if c.isalnum())
    if not 15 <= len(s) <= 34 or not s[:2].isalpha() or not s[2:4].isdigit():
        return False
    u = s[4:] + s[:4]
    num = "".join(str(ord(c) - 55) if c.isalpha() else c for c in u)
    return num.isdigit() and int(num) % 97 == 1


def verproben(felder, zweite=None):
    heute = datetime.date.today()
    regeln = {}
    d = felder.get("datum")
    try:
        dt = datetime.date.fromisoformat(d) if d else None
        regeln["R1_datum"] = bool(dt and datetime.date(2015, 1, 1) <= dt <= heute.replace(year=heute.year + 1))
    except ValueError:
        regeln["R1_datum"] = False
    b, n, s = felder.get("brutto"), felder.get("netto"), felder.get("steuer")
    regeln["R2_brutto"] = bool(b and b > 0)
    if b and n is not None and s is not None:
        regeln["R3_summen"] = abs((n + s) - b) <= 0.02
    elif n and s is not None:
        regeln["R3_summen"] = any(abs(n * satz - s) <= max(0.05, n * 0.002) for satz in SAETZE)
    elif b and n and s is None:
        regeln["R3_summen"] = any(abs(n * (1 + satz) - b) <= max(0.05, b * 0.002) for satz in SAETZE)
    else:
        regeln["R3_summen"] = None
    regeln["R4_cuit"] = cuit_ok(felder["cuit"]) if felder.get("cuit") else None
    regeln["R5_iban"] = iban_ok(felder["iban"]) if felder.get("iban") else None
    regeln["R6_ustid"] = bool(felder.get("ust_id") and len(felder["ust_id"]) == 11) if felder.get("ust_id") else None
    if zweite and zweite.get("brutto") and b:
        regeln["R7_zwei_lesungen"] = abs(zweite["brutto"] - b) <= 0.02
    else:
        regeln["R7_zwei_lesungen"] = None
    gruende = []
    if not regeln["R1_datum"]:
        gruende.append("date missing or implausible")
    if not regeln["R2_brutto"]:
        gruende.append("gross amount missing")
    if regeln["R3_summen"] is False:
        gruende.append("totals do not add up (net+tax != gross)")
    if regeln["R4_cuit"] is False:
        gruende.append("CUIT check digit wrong")
    if regeln["R5_iban"] is False:
        gruende.append("IBAN check digits wrong")
    if regeln["R6_ustid"] is False:
        gruende.append("VAT ID incomplete")
    if regeln["R7_zwei_lesungen"] is False:
        gruende.append("two readings disagree on the gross amount")
    if regeln["R3_summen"] is None and regeln["R2_brutto"]:
        gruende.append("totals cannot be checked (net/tax not found)")
    bestanden = regeln["R1_datum"] and regeln["R2_brutto"] and regeln["R3_summen"] is not False and regeln["R4_cuit"] is not False and regeln["R5_iban"] is not False and regeln["R6_ustid"] is not False and regeln["R7_zwei_lesungen"] is not False
    sicher = bestanden and regeln["R3_summen"] is True
    return {"bestanden": bool(bestanden), "sicher": bool(sicher), "regeln": regeln, "gruende": gruende}


def verproben_zwilling(mandant, zwilling_pfad, protokollieren=True):
    with open(zwilling_pfad, encoding="utf-8") as f:
        z = json.load(f)
    v = verproben(z.get("felder", {}), z.get("zweite_lesung"))
    zeile = {"zeit": bp._jetzt(), "datei": z.get("datei"), "sha256": z.get("sha256"), "methode": z.get("methode"), "felder": z.get("felder"), **v}
    pl = os.path.join(bp.WURZEL, mandant, "pruefliste.jsonl")
    os.makedirs(os.path.dirname(pl), exist_ok=True)
    with open(pl, "a", encoding="utf-8") as f:
        f.write(json.dumps(zeile, ensure_ascii=False) + "\n")
    if protokollieren:
        bp.schreiben(bp.pfad_fuer(mandant), ereignis="verprobt", quelle=z.get("datei", ""), ziel=zwilling_pfad, sha256=z.get("sha256", ""), werkzeug="verprobung.py", mandant=mandant,
                     meldung=("bestanden" + (" (sicher)" if v["sicher"] else "")) if v["bestanden"] else ("Needs review: " + "; ".join(v["gruende"])), bestanden=v["bestanden"], sicher=v["sicher"], regeln=v["regeln"])
    return zeile


def main():
    ap = argparse.ArgumentParser(description="Trove F4 Verprobung")
    ap.add_argument("zwilling", help="Zwilling-JSON aus der Lese-Treppe")
    ap.add_argument("--mandant", default="haus")
    ap.add_argument("--ohne-protokoll", action="store_true")
    a = ap.parse_args()
    z = verproben_zwilling(a.mandant, a.zwilling, not a.ohne_protokoll)
    print(json.dumps({k: z[k] for k in ("datei", "methode", "bestanden", "sicher", "gruende", "regeln")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
