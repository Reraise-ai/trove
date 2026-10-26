"""Trove: eine PDF-Seite als PNG fuer die Ansicht im Tab (nur lesen). Aufruf: python pdf_seite.py <pdf> <seite ab 0>
Ausgabe (letzte Zeile): {"seiten": N, "seite": k, "mime": "image/png", "data": "data:image/png;base64,..."}"""
import base64, io, json, sys

import pypdfium2 as pdfium  # Apache-2.0/BSD, ersetzt pymupdf (AGPL) fuers Rendern

try:
    doc = pdfium.PdfDocument(sys.argv[1])
    n = len(doc)
except Exception:
    sys.exit("PDF is encrypted or empty")
if n == 0:
    sys.exit("PDF is encrypted or empty")
seite = min(max(int(sys.argv[2]), 0), n - 1)
bild = doc[seite].render(scale=1.6).to_pil()
buf = io.BytesIO(); bild.save(buf, format="PNG"); png = buf.getvalue()
print(json.dumps({"seiten": n, "seite": seite, "mime": "image/png",
                  "data": "data:image/png;base64," + base64.b64encode(png).decode()}))
