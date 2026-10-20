"""Trove F6 — Belegstapel zerlegen: ein PDF mit vielen Belegen (Scanner-Stapel, 35 Seiten, 15 Rechnungen) wird in
einzelne Belege geschnitten. Begleitseiten (Lieferschein, AGB, Anschreiben) bleiben beim Beleg, gehen aber nicht in die
Buchung: je Beleg entsteht die volle Datei (alle Seiten) und eine Buchungsfassung (nur Rechnungsseiten).
Schneiden macht Stirling-PDF (A-05, laeuft auf dem Server); Seiten lesen macht die Lese-Treppe (F3); Buchungsfassung
leitet pymupdf aus dem geschnittenen Beleg ab. Das Original bleibt unveraendert. Jeder Beleg wird im Belegprotokoll
als "zerlegt" festgehalten (Quelle = Stapel, Ziel = Beleg, Pruefsummen).
"""
import argparse, hashlib, io, json, os, re, sys, time, urllib.request, uuid, zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import belegprotokoll as bp
import lese_treppe as lt

STIRLING = os.environ.get("TROVE_STIRLING", "http://127.0.0.1:8090")

R_RECHNUNG = re.compile(r"\b(Rechnung|Invoice|Factura|Gutschrift|Credit\s*note|Nota\s+de\s+cr[eé]dito|Abrechnung|Quittung|Receipt|Recibo)\b", re.I)
R_NUMMER = re.compile(r"(?:Rechnungs-?\s?(?:Nr|nummer)\.?|Invoice\s?(?:No|Number|#)\.?|Factura\s?(?:N[°ro]+\.?|Nro\.?)|Comp\.?\s?Nro\.?|Beleg-?Nr\.?|Nr\.|N°)\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-\.]{2,25})", re.I)
R_SEITE = re.compile(r"\b(?:Seite|Page|P[aá]gina)\s*(\d{1,3})\s*(?:von|of|de|/)\s*(\d{1,3})\b", re.I)
R_LIEFER = re.compile(r"\b(Lieferschein|Delivery\s*note|Remito|Packing\s*list|Lieferavis)\b", re.I)
R_AGB = re.compile(r"(Allgemeine\s+Gesch[aä]ftsbedingungen|\bAGB\b|Terms\s+(?:and|&)\s+Conditions|T[eé]rminos\s+y\s+condiciones|Condiciones\s+generales|Widerrufsbelehrung)", re.I)
R_ANSCHREIBEN = re.compile(r"\b(Sehr\s+geehrte|Dear\s+(?:Sir|Madam|Mr|Ms|Mrs)|Estimad[oa]s?)\b", re.I)


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def seiten_lesen(pfad, ocr="rapidocr"):
    import fitz
    doc = fitz.open(pfad)
    seiten = []
    for i, page in enumerate(doc):
        text = page.get_text("text")
        methode = "textlayer"
        if len(text.strip()) < lt.MIN_ZEICHEN_JE_SEITE:
            try:
                text = lt.OCR[ocr](lt.seite_als_png(page)); methode = "ocr:" + ocr
            except Exception as ex:
                methode = "fehler:" + str(ex)[:60]
        seiten.append({"nr": i + 1, "text": text, "methode": methode})
    doc.close()
    return seiten


def einordnen(seite):
    t = seite["text"]
    art = "sonstig"
    m = R_SEITE.search(t)
    n, von = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    nummer = None
    for mn in R_NUMMER.finditer(t):
        davor = t[max(0, mn.start() - 14):mn.start()].lower()
        if any(w in davor for w in ("id", "kunden", "steuer", "cuit", "ust", "vat", "tax", "konto", "tel")) or not re.search(r"\d", mn.group(1)):
            continue  # Beschriftung statt Wert (z. B. "Rechnungsnr.\nKunden-Referenz" bei Spaltenlayout)
        nummer = mn.group(1).strip("."); break
    if R_AGB.search(t) and not R_RECHNUNG.search(t[:400]):
        art = "agb"
    elif R_LIEFER.search(t) and not R_NUMMER.search(t[:300]):
        art = "lieferschein"
    elif R_RECHNUNG.search(t):
        art = "folgeseite" if (n and n > 1) else "rechnung"
    elif R_ANSCHREIBEN.search(t):
        art = "anschreiben"
    elif n and n > 1:
        art = "folgeseite"
    return {"nr": seite["nr"], "art": art, "seite_n": n, "seite_von": von, "nummer": nummer, "methode": seite["methode"], "zeichen": len(t.strip())}


def gruppieren(klassen):
    """Belege bilden: neuer Beleg bei einer Rechnungs-Startseite (Seite 1 oder neue Nummer); alles andere haengt am
    laufenden Beleg. Seiten vor dem ersten Start bilden einen Vorlauf-Beleg."""
    gruppen = []
    akt = None
    def erwartet_mehr(g):
        s = g["seiten"][-1]
        return bool(s["seite_n"] and s["seite_von"] and s["seite_n"] < s["seite_von"])
    for k in klassen:
        start = k["art"] == "rechnung" and (akt is None or k["seite_n"] == 1 or (k["nummer"] and k["nummer"] != akt.get("nummer")) or not erwartet_mehr(akt))
        if start or akt is None:
            akt = {"nummer": k["nummer"], "seiten": []}
            gruppen.append(akt)
        if k["nummer"] and not akt.get("nummer"):
            akt["nummer"] = k["nummer"]
        akt["seiten"].append(k)
    for g in gruppen:
        g["buchungsseiten"] = [s["nr"] for s in g["seiten"] if s["art"] in ("rechnung", "folgeseite", "sonstig")]
        g["anhangseiten"] = [s["nr"] for s in g["seiten"] if s["art"] in ("lieferschein", "agb", "anschreiben")]
        g["von"], g["bis"] = g["seiten"][0]["nr"], g["seiten"][-1]["nr"]
    return gruppen


def stirling_split(pdf_bytes, schnitte):
    """Stirling-PDF /api/v1/general/split-pages: Schnitt NACH den genannten Seiten. Liefert Liste von PDF-Bytes in Reihenfolge."""
    boundary = "----trove" + uuid.uuid4().hex
    teile = []
    teile.append(("--%s\r\nContent-Disposition: form-data; name=\"fileInput\"; filename=\"stapel.pdf\"\r\nContent-Type: application/pdf\r\n\r\n" % boundary).encode() + pdf_bytes + b"\r\n")
    teile.append(("--%s\r\nContent-Disposition: form-data; name=\"pageNumbers\"\r\n\r\n%s\r\n" % (boundary, ",".join(str(s) for s in schnitte) or "0")).encode())
    teile.append(("--%s--\r\n" % boundary).encode())
    body = b"".join(teile)
    req = urllib.request.Request(STIRLING + "/api/v1/general/split-pages", data=body, headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=600) as h:
        daten = h.read(); ct = h.headers.get("Content-Type", "")
    if "zip" in ct or daten[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(daten))
        namen = sorted(z.namelist(), key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)])
        return [z.read(n) for n in namen if n.lower().endswith(".pdf")]
    return [daten]


def zerlegen(pfad, mandant="haus", ocr="rapidocr", ausgabe=None, protokollieren=True):
    import fitz
    t0 = time.time()
    with open(pfad, "rb") as f:
        roh = f.read()
    stapel_sha = sha_bytes(roh)
    seiten = seiten_lesen(pfad, ocr)
    klassen = [einordnen(s) for s in seiten]
    gruppen = gruppieren(klassen)
    schnitte = [g["bis"] for g in gruppen[:-1]]
    teile = stirling_split(roh, schnitte) if schnitte else [roh]
    if len(teile) != len(gruppen):
        raise RuntimeError("Stirling lieferte %d Teile, erwartet %d" % (len(teile), len(gruppen)))
    ziel = ausgabe or os.path.join(bp.WURZEL, mandant, "stapel", stapel_sha[:12])
    os.makedirs(ziel, exist_ok=True)
    manifest = {"stapel": pfad, "stapel_sha256": stapel_sha, "seiten": len(seiten), "belege": [], "zeit": bp._jetzt(), "werkzeug": "stapel.py + Stirling-PDF " + STIRLING}
    prot = bp.Protokoll(bp.pfad_fuer(mandant)) if protokollieren else None
    try:
        if prot:
            prot.schreiben("notiz", quelle=pfad, sha256=stapel_sha, werkzeug="stapel.py", mandant=mandant, lauf="stapel-" + stapel_sha[:12], meldung="Stapel wird zerlegt: %d Seiten -> %d Belege" % (len(seiten), len(gruppen)))
        for i, (g, pdf) in enumerate(zip(gruppen, teile), 1):
            name = "beleg_%02d%s.pdf" % (i, ("_" + re.sub(r"[^A-Za-z0-9\-]", "_", g["nummer"])[:24]) if g.get("nummer") else "")
            p = os.path.join(ziel, name)
            with open(p, "wb") as f:
                f.write(pdf)
            d = fitz.open(p)
            if len(d) != len(g["seiten"]):
                raise RuntimeError("Beleg %d: %d Seiten geschnitten, %d erwartet" % (i, len(d), len(g["seiten"])))
            # Buchungsfassung: nur Rechnungsseiten (Lieferschein/AGB/Anschreiben bleiben nur in der vollen Datei)
            buch_pfad = None
            if g["anhangseiten"] and g["buchungsseiten"]:
                idx = [s - g["von"] for s in g["buchungsseiten"]]
                d.select(idx); buch_pfad = p[:-4] + ".buchung.pdf"; d.save(buch_pfad)
            d.close()
            eintrag = {"nr": i, "datei": p, "sha256": lt.sha_datei(p), "seiten": [g["von"], g["bis"]], "nummer": g.get("nummer"),
                       "arten": [s["art"] for s in g["seiten"]], "buchungsseiten": g["buchungsseiten"], "anhangseiten": g["anhangseiten"], "buchungsfassung": buch_pfad,
                       "buchungsfassung_sha256": lt.sha_datei(buch_pfad) if buch_pfad else None}
            manifest["belege"].append(eintrag)
            if prot:
                prot.schreiben("zerlegt", quelle=pfad, ziel=p, sha256=eintrag["sha256"], bytes_=os.path.getsize(p), werkzeug="stapel.py/stirling", mandant=mandant, lauf="stapel-" + stapel_sha[:12],
                               meldung="Beleg %d von %d: Seiten %d-%d, Arten %s%s" % (i, len(gruppen), g["von"], g["bis"], "/".join(eintrag["arten"]), (", Buchungsfassung ohne Anhangseiten %s" % g["anhangseiten"]) if buch_pfad else ""),
                               stapel_sha256=stapel_sha, seiten=eintrag["seiten"], nummer=g.get("nummer"), buchungsfassung=buch_pfad, buchungsfassung_sha256=eintrag["buchungsfassung_sha256"])
        manifest["dauer_s"] = round(time.time() - t0, 2)
        with open(os.path.join(ziel, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        if prot:
            prot.anker_setzen(mandant, "stapel-" + stapel_sha[:12])
    finally:
        if prot:
            prot.close()
    return manifest


def main():
    ap = argparse.ArgumentParser(description="Trove F6 Belegstapel zerlegen (Stirling-PDF)")
    ap.add_argument("stapel")
    ap.add_argument("--mandant", default="haus")
    ap.add_argument("--ocr", default="rapidocr")
    ap.add_argument("--ausgabe")
    ap.add_argument("--nur-einordnen", action="store_true", help="nur zeigen, wie die Seiten eingeordnet wuerden")
    ap.add_argument("--ohne-protokoll", action="store_true")
    ap.add_argument("--json", action="store_true", help="Manifest als JSON ausgeben (fuer das Plugin)")
    a = ap.parse_args()
    if a.nur_einordnen:
        for k in [einordnen(s) for s in seiten_lesen(a.stapel, a.ocr)]:
            print(k)
        return
    m = zerlegen(a.stapel, a.mandant, a.ocr, a.ausgabe, not a.ohne_protokoll)
    if a.json:
        print(json.dumps(m, ensure_ascii=False)); return
    print("Stapel %s: %d Seiten -> %d Belege in %.1fs" % (os.path.basename(a.stapel), m["seiten"], len(m["belege"]), m["dauer_s"]))
    for b in m["belege"]:
        print("  %02d Seiten %d-%d %-28s %s%s" % (b["nr"], b["seiten"][0], b["seiten"][1], (b["nummer"] or "-")[:28], "/".join(b["arten"]), "  [Buchungsfassung ohne %s]" % b["anhangseiten"] if b["buchungsfassung"] else ""))


if __name__ == "__main__":
    main()
