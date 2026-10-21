"""Trove: eine PDF-Seite als PNG fuer die Ansicht im Tab (nur lesen). Aufruf: python pdf_seite.py <pdf> <seite ab 0>
Ausgabe (letzte Zeile): {"seiten": N, "seite": k, "mime": "image/png", "data": "data:image/png;base64,..."}"""
import base64, json, sys

import fitz  # pymupdf

doc = fitz.open(sys.argv[1])
if doc.needs_pass or len(doc) == 0:
    sys.exit("PDF ist verschluesselt oder leer")
seite = min(max(int(sys.argv[2]), 0), len(doc) - 1)
png = doc[seite].get_pixmap(matrix=fitz.Matrix(1.6, 1.6)).tobytes("png")
print(json.dumps({"seiten": len(doc), "seite": seite, "mime": "image/png", "data": "data:image/png;base64," + base64.b64encode(png).decode()}))
