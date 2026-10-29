"""Trove F3 — Lese-Treppe: macht einen Beleg maschinenlesbar, so billig wie moeglich.

Stufen (Plan §5): 1 E-Rechnung (eingebettetes XML, ZUGFeRD/Factur-X) → 2 Textlayer (PDF-Text) → 3 OCR (lokal, kein bezahlter Token).
OCR-Kandidaten (A-06, per Messung gewaehlt): rapidocr (ONNX, CPU) · docling (ibm/granite-docling ueber Ollama)
· granite-vision (granite3.2-vision ueber Ollama). Ergebnis je Beleg: Zwilling-JSON unter <TROVE_DATEN>\\<mandant>\\zwillinge\\<sha>.json
(Text, Methode, Dauer, ausgelesene Felder). Die Verprobung (F4) steht in verprobung.py; sie entscheidet, nicht die KI.
"""
import argparse, base64, hashlib, io, json, os, re, sys, time, urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import belegprotokoll as bp

OLLAMA = os.environ.get("TROVE_OLLAMA", "http://127.0.0.1:11434")
MIN_ZEICHEN_JE_SEITE = 80
DPI = 110
_rapid = None


def sha_datei(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ---------- Stufe 1: E-Rechnung ----------

def e_rechnung(pfad):
    """Eingebettetes XML (ZUGFeRD/Factur-X/XRechnung) auslesen. Liefert Text + Felder oder None."""
    import pikepdf
    try:
        pdf = pikepdf.open(pfad)
        namen = list(pdf.attachments.keys())
    except Exception:
        return None
    for n in namen:
        if not n.lower().endswith(".xml"):
            continue
        try:
            roh = pdf.attachments[n].get_file().read_bytes()
            root = ET.fromstring(roh)
        except Exception:
            continue
        txt = ET.tostring(root, encoding="unicode", method="text")
        felder = {}
        def such(tag):
            for el in root.iter():
                if el.tag.split("}")[-1] == tag and (el.text or "").strip():
                    return el.text.strip()
            return None
        felder["rechnungsnummer"] = such("ID")
        felder["brutto"] = _zahl(such("GrandTotalAmount") or such("DuePayableAmount"))
        felder["netto"] = _zahl(such("TaxBasisTotalAmount") or such("LineTotalAmount"))
        felder["steuer"] = _zahl(such("TaxTotalAmount"))
        felder["waehrung"] = such("InvoiceCurrencyCode")
        d = such("DateTimeString")
        if d and re.fullmatch(r"\d{8}", d):
            felder["datum"] = "%s-%s-%s" % (d[:4], d[4:6], d[6:])
        pdf.close()
        return {"methode": "e-rechnung", "text": txt, "felder": {k: v for k, v in felder.items() if v}, "xml_name": n}
    pdf.close()
    return None


# ---------- Stufe 2: Textlayer ----------

def textlayer(doc):
    seiten = [doc[i].get_textpage().get_text_bounded() for i in range(len(doc))]
    gesamt = "\n\f".join(seiten)
    zeichen = sum(len(s.strip()) for s in seiten)
    return gesamt, zeichen / max(1, len(seiten))


# ---------- Stufe 3: OCR ----------

def seite_als_png(page, dpi=DPI):
    bild = page.render(scale=dpi / 72.0).to_pil().convert("RGB")
    buf = io.BytesIO(); bild.save(buf, format="PNG")
    return buf.getvalue()


def ocr_rapid(png_bytes):
    global _rapid
    if _rapid is None:
        from rapidocr_onnxruntime import RapidOCR
        _rapid = RapidOCR()
    import numpy as np
    from PIL import Image
    img = np.array(Image.open(io.BytesIO(png_bytes)).convert("RGB"))
    res, _ = _rapid(img)
    if not res:
        return ""
    zeilen = sorted(res, key=lambda r: (round(r[0][0][1] / 12), r[0][0][0]))
    return "\n".join(r[1] for r in zeilen)


def ocr_ollama(png_bytes, modell, prompt):
    body = json.dumps({"model": modell, "prompt": prompt, "images": [base64.b64encode(png_bytes).decode()], "stream": False,
                       "options": {"temperature": 0, "num_predict": 2048}}).encode()
    req = urllib.request.Request(OLLAMA + "/api/generate", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as h:
        return json.loads(h.read())["response"]


OCR = {
    "rapidocr": lambda png: ocr_rapid(png),
    "docling": lambda png: ocr_ollama(png, "ibm/granite-docling", "Convert this page to docling."),
    "granite-vision": lambda png: ocr_ollama(png, "granite3.2-vision", "Transcribe all text on this document page exactly as written, line by line. Output only the text."),
}


# ---------- Felder (feste Regeln, keine KI) ----------

MONATE = {"jan": 1, "feb": 2, "mar": 3, "mär": 3, "apr": 4, "mai": 5, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "set": 9, "okt": 10, "oct": 10, "nov": 11, "dez": 12, "dic": 12, "dec": 12, "ene": 1, "abr": 4, "ago": 8}


def _zahl(s):
    if s is None:
        return None
    s = str(s).strip().replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(".", "").replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    elif re.search(r"\.\d{3}$", s) and len(s) > 4:
        s = s.replace(".", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


BETRAG = r"([-]?\$?\s?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})|\d+[.,]\d{2})(?!\s*%)"


def felder_aus_text(text):
    t = re.sub(r"<loc_\d+>|</?doc>|</?[a-z_]+>", " ", text).replace("\xa0", " ")
    f = {}
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", t)
    m2 = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    m3 = re.search(r"\b(\d{1,2})\.?\s+([A-Za-zäÄ]{3,9})\.?\s+(\d{4})\b", t)
    if m2:
        f["datum"] = "%s-%s-%s" % m2.groups()
    elif m:
        d, mo, y = m.groups(); y = ("20" + y) if len(y) == 2 else y
        if int(mo) > 12 and int(d) <= 12:
            d, mo = mo, d  # US-Schreibweise m/d/y
        if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            f["datum"] = "%s-%02d-%02d" % (y, int(mo), int(d))
    elif m3 and m3.group(2)[:3].lower() in MONATE:
        f["datum"] = "%s-%02d-%02d" % (m3.group(3), MONATE[m3.group(2)[:3].lower()], int(m3.group(1)))
    m = re.search(r"C\.?U\.?I\.?[TL]\.?\s*[:#Nº°]*\s*(\d{2})-?(\d{8})-?(\d)\b", t, re.I) or re.search(r"\b(\d{2})-(\d{8})-(\d)\b", t)
    if m:
        f["cuit"] = "%s-%s-%s" % m.groups()
    m = re.search(r"\b(DE\s?\d{9})\b", t)
    if m:
        f["ust_id"] = m.group(1).replace(" ", "")
    m = re.search(r"\b([A-Z]{2}\d{2}(?:[ \t]?[A-Z0-9]{4}){2,7}[ \t]?[A-Z0-9]{1,4})\b", t)
    if m and m.group(1)[:2] in ("DE", "AT", "CH", "ES", "NL", "FR", "IT"):
        f["iban"] = m.group(1).replace(" ", "")
    m = re.search(r"(?:Rechnungs-?\s?(?:Nr|nummer)\.?|Invoice\s?(?:No|Number)\.?|Factura\s?(?:N[°ro]+\.?|Nro\.?)|Comp\.?\s?Nro\.?|N°)\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-\.]{2,25})", t, re.I)
    if m and re.search(r"\d", m.group(1)):  # Wert muss eine Ziffer enthalten, sonst war es die naechste Beschriftung
        f["rechnungsnummer"] = m.group(1)
    if re.search(r"\bEUR\b|€", t):
        f["waehrung"] = "EUR"
    elif re.search(r"\bARS\b|\$", t):
        f["waehrung"] = "ARS"
    elif re.search(r"\bUSD\b|US\$", t):
        f["waehrung"] = "USD"
    def betrag_nach(muster):
        best = None
        for m in re.finditer(muster + r"[^\d\-]{0,25}" + BETRAG, t, re.I):
            v = _zahl(m.group(1).replace("$", ""))
            if v is not None and (best is None or v > best):
                best = v
        return best
    f["brutto"] = betrag_nach(r"(?:Importe\s+Total|Total\s+a\s+pagar|Gesamtbetrag|Bruttobetrag|Rechnungsbetrag|Zahlbetrag|Endbetrag|Gross\s+total|Grand\s+total|Amount\s+due|TOTAL)")
    f["netto"] = betrag_nach(r"(?:Nettobetrag|Netto|Subtotal|Sub-total|Importe\s+Neto\s+Gravado|Neto|Net\s+amount|Zwischensumme)")
    f["steuer"] = betrag_nach(r"(?:IVA\s*(?:21|10[.,]5|27)?\s*%?|MwSt\.?|USt\.?|Umsatzsteuer|VAT|Mehrwertsteuer)")
    if f["brutto"] is None:
        zahlen = [v for v in (_zahl(x.replace("$", "")) for x in re.findall(BETRAG, t)) if v is not None]
        if zahlen:
            f["brutto"] = max(zahlen); f["brutto_quelle"] = "groesster betrag"
    return {k: v for k, v in f.items() if v is not None}


# ---------- Treppe ----------

def bilder_rendern(doc, n, dpi):
    """First n pages as base64 PNG for a recognition model (erkennung.py); returned in the JSON output, never stored in the twin."""
    return [base64.b64encode(seite_als_png(doc[i], dpi)).decode() for i in range(min(n, len(doc)))]


def lesen(pfad, ocr="rapidocr", erzwinge_ocr=False, max_seiten=10, seitenbilder=0, bilder_alle=False, ohne_ocr=False, bild_dpi=DPI):
    """seitenbilder: also return the first N page images (scans; with bilder_alle for every PDF). ohne_ocr: for scans skip the local
    OCR and leave the reading to the OCR model of erkennung.py (methode "ocr:ausstehend")."""
    import pypdfium2 as pdfium
    t0 = time.time()
    doc = pdfium.PdfDocument(pfad)
    anz = len(doc)
    seiten = min(anz, max_seiten)
    erg = {"datei": pfad, "sha256": sha_datei(pfad), "seiten": anz, "gelesene_seiten": seiten, "stufen": []}
    if not erzwinge_ocr:
        er = e_rechnung(pfad)
        if er:
            erg.update(methode="e-rechnung", text=er["text"], felder=er["felder"], e_rechnung_xml=er["xml_name"])
            erg["stufen"].append("e-rechnung")
            if bilder_alle and seitenbilder:
                erg["_seitenbilder"] = bilder_rendern(doc, min(seiten, seitenbilder), bild_dpi)
            erg["dauer_s"] = round(time.time() - t0, 3)
            doc.close(); return erg
        text, je_seite = textlayer(doc)
        erg["stufen"].append("textlayer")
        if je_seite >= MIN_ZEICHEN_JE_SEITE:
            erg.update(methode="textlayer", text=text, felder=felder_aus_text(text), zeichen_je_seite=round(je_seite))
            if bilder_alle and seitenbilder:
                erg["_seitenbilder"] = bilder_rendern(doc, min(seiten, seitenbilder), bild_dpi)
            erg["dauer_s"] = round(time.time() - t0, 3)
            doc.close(); return erg
        erg["zeichen_je_seite_textlayer"] = round(je_seite)
    if ohne_ocr:
        erg.update(methode="ocr:ausstehend", text="", felder={}, ocr_ausstehend=True)
        erg["stufen"].append("ocr:ausstehend")
        erg["_seitenbilder"] = bilder_rendern(doc, min(seiten, max(1, seitenbilder)), bild_dpi)
        erg["dauer_s"] = round(time.time() - t0, 3)
        doc.close(); return erg
    teile = []
    t_ocr = time.time()
    for i in range(seiten):
        png = seite_als_png(doc[i])
        teile.append(OCR[ocr](png))
    if seitenbilder:
        erg["_seitenbilder"] = bilder_rendern(doc, min(seiten, seitenbilder), bild_dpi)
    text = "\n\f".join(teile)
    erg["stufen"].append("ocr:" + ocr)
    erg.update(methode="ocr:" + ocr, text=text, felder=felder_aus_text(text), ocr_dauer_s=round(time.time() - t_ocr, 3), ocr_s_je_seite=round((time.time() - t_ocr) / max(1, seiten), 3))
    erg["dauer_s"] = round(time.time() - t0, 3)
    doc.close()
    return erg


def zwilling_speichern(mandant, erg):
    d = os.path.join(bp.WURZEL, mandant, "zwillinge")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, erg["sha256"] + ".json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(erg, f, ensure_ascii=False, indent=1)
    return p


def main():
    ap = argparse.ArgumentParser(description="Trove F3 Lese-Treppe")
    ap.add_argument("datei")
    ap.add_argument("--mandant", default="haus")
    ap.add_argument("--ocr", default="rapidocr", choices=sorted(OCR))
    ap.add_argument("--erzwinge-ocr", action="store_true")
    ap.add_argument("--ohne-protokoll", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-seiten", type=int, default=10)
    ap.add_argument("--seitenbilder", type=int, default=0, help="also return the first N page images (base64 PNG) in the JSON output (scans)")
    ap.add_argument("--bilder-alle", action="store_true", help="page images for every PDF, not only scans")
    ap.add_argument("--ohne-ocr", action="store_true", help="scans: no local OCR, the OCR model of erkennung.py reads the page images")
    ap.add_argument("--bild-dpi", type=int, default=DPI)
    a = ap.parse_args()
    erg = lesen(a.datei, a.ocr, a.erzwinge_ocr, a.max_seiten, max(0, min(a.seitenbilder, 20)), a.bilder_alle, a.ohne_ocr, max(50, min(a.bild_dpi, 300)))
    bilder = erg.pop("_seitenbilder", None)
    zp = zwilling_speichern(a.mandant, erg)
    if not a.ohne_protokoll and not erg.get("ocr_ausstehend"):  # pending OCR: erkennung.py logs "gelesen" after the model read it
        bp.schreiben(bp.pfad_fuer(a.mandant), ereignis="gelesen", quelle=a.datei, ziel=zp, sha256=erg["sha256"], werkzeug="lese_treppe.py", mandant=a.mandant,
                     meldung="%s, %d Seiten, %.2fs" % (erg["methode"], erg["seiten"], erg["dauer_s"]), methode=erg["methode"], felder=erg["felder"])
    if a.json:
        print(json.dumps({**{k: v for k, v in erg.items() if k != "text"}, "zwilling": zp, "text_anfang": (erg.get("text") or "")[:1500],
                          **({"seitenbilder": bilder} if bilder else {})}, ensure_ascii=False))
    else:
        print("%s | %s | %d Seiten | %.2fs | Felder: %s" % (os.path.basename(a.datei), erg["methode"], erg["seiten"], erg["dauer_s"], json.dumps(erg["felder"], ensure_ascii=False)))


if __name__ == "__main__":
    main()
