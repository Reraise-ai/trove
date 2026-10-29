#!/usr/bin/env python3
"""Recognition model tests (erkennung.py). No real model service: a fake Ollama / OpenAI-compatible server on 127.0.0.1 and a
Hermes mock for the provider path. Checks: off = today's reading (one plain lese_treppe call, no network); pinning (model id + Ollama
digest; OCR stage also layers and GGUF file sha256); a changed digest, a missing model or an answer from another model is refused,
never replaced; fallback rules marks the result, fallback none is an error; values not in the source text are dropped and reported;
provider mode needs the opt-in and goes through Hermes (Trove never sees a key); OCR stage for scans and as second reading (R7);
the endpoint key never reaches a twin, a log line, a response or an error text; the MCP server reads with the same configured model
(settings from Hermes' config.yaml) and offers no tool that changes the model."""
import asyncio
import base64
import http.server
import importlib.util
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import types
import warnings

warnings.filterwarnings("ignore")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(REPO, "plugin", "dashboard")
TMP = tempfile.mkdtemp(prefix="trove-erk-")
os.environ.update(TROVE_DATEN=os.path.join(TMP, "daten"), TROVE_KONFIG=os.path.join(TMP, "daten", "trove.json"), TROVE_MANDANT="t",
                  TROVE_PYTHON=sys.executable, HERMES_HOME=os.path.join(TMP, "hermes"))
os.makedirs(os.environ["TROVE_DATEN"])
os.makedirs(os.environ["HERMES_HOME"])
sys.path.insert(0, DASH)
import zugang  # noqa: E402
import konfig  # noqa: E402
import erkennung as ek  # noqa: E402
import verprobung as vp  # noqa: E402

SCHLUESSEL = "sk-LOCAL-ZZ91x7Qp44rT"
LOGS = []


class _H(logging.Handler):
    def emit(self, r):
        LOGS.append(r.getMessage())


logging.getLogger().addHandler(_H())
logging.getLogger().setLevel(logging.DEBUG)

# ---------- fake model server ----------
Z = {}


def zustand_neu():
    Z.clear()
    Z.update(ollama=True, modelle={"fields-7b": {"digest": "a" * 64, "caps": ["completion"]},
                                   "ocr-3b": {"digest": "c" * 64, "caps": ["completion", "vision"]}},
             schichten={"ocr-3b": ["1" * 64, "2" * 64]}, antwort={}, antwort_modell=None, ocr_text="", anfragen=[], koepfe=[])


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        Z["koepfe"].append(dict(self.headers))
        if self.path == "/api/tags" and Z["ollama"]:
            return self._json(200, {"models": [{"name": n, "model": n, "digest": m["digest"], "size": 1, "capabilities": m["caps"]} for n, m in Z["modelle"].items()]})
        if self.path == "/v1/models" and not Z["ollama"]:
            return self._json(200, {"data": [{"id": n} for n in Z["modelle"]]})
        self._json(404, {"error": "ZZBODYMARKZZ " + SCHLUESSEL})

    def do_POST(self):
        Z["koepfe"].append(dict(self.headers))
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        Z["anfragen"].append((self.path, body))
        modell = Z["antwort_modell"] or body.get("model")
        if self.path == "/api/show":
            return self._json(200, {"modelfile": "".join("FROM /x/blobs/sha256-%s\n" % h for h in Z["schichten"].get(body.get("model"), []))})
        if self.path == "/api/chat" and body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            try:
                for z in Z["ocr_text"].splitlines(True):
                    self.wfile.write((json.dumps({"model": modell, "message": {"content": z}, "done": False}) + "\n").encode())
                self.wfile.write((json.dumps({"model": modell, "message": {"content": ""}, "done": True}) + "\n").encode())
            except OSError:
                pass  # the client stopped reading (repetition detected)
            return
        if self.path == "/api/chat":
            return self._json(200, {"model": modell, "message": {"content": json.dumps(Z["antwort"])}})
        if self.path == "/v1/chat/completions":
            return self._json(200, {"model": modell, "choices": [{"message": {"content": "```json\n" + json.dumps(Z["antwort"]) + "\n```"}}]})
        self._json(500, {"error": "ZZBODYMARKZZ"})


SERVER = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=SERVER.serve_forever, daemon=True).start()
EP = "http://127.0.0.1:%d" % SERVER.server_address[1]
S = {}
zugang.einstellungen_quelle = lambda: dict(S)
zugang.geheimnis_quelle = lambda n: SCHLUESSEL if n == zugang.ERKENNUNG_KEY else None


def setze(**kw):
    S.clear()
    S.update({"erkennung_endpoint": EP, **kw})
    zugang.neu_laden()


def pins_weg():
    k = konfig.lesen()
    k.pop(ek.PIN_SCHLUESSEL, None)
    k.pop(ek.PIN_OCR, None)
    with open(konfig.konfig_pfad(), "w", encoding="utf-8") as f:
        json.dump(k, f)


def fehler_art(f):
    try:
        f()
    except ek.ErkennungFehler as ex:
        return ex.art, str(ex)
    raise AssertionError("no ErkennungFehler")


TEXT = "ACME GmbH\nRechnung Nr. RE-77\nDatum 03.02.2026\nNetto 4.200,00 EUR\nMwSt 19 % 798,00 EUR\nGesamtbetrag 4.998,00 EUR\nDE123456789\n"
ZW = {"methode": "textlayer", "text": TEXT, "felder": {"datum": "2026-02-03", "brutto": 4998.0, "netto": 12.34, "waehrung": "EUR"}}

# ---------- PDFs ----------


def text_pdf(pfad, zeilen):
    inhalt = "BT /F1 11 Tf 50 780 Td 14 TL " + " ".join("(%s) '" % z for z in zeilen) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", "<< /Length %d >>\nstream\n%s\nendstream" % (len(inhalt), inhalt)]
    out = b"%PDF-1.4\n"
    pos = []
    for i, o in enumerate(objs, 1):
        pos.append(len(out))
        out += ("%d 0 obj\n%s\nendobj\n" % (i, o)).encode("latin-1")
    x = len(out)
    out += ("xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)).encode() + "".join("%010d 00000 n \n" % p for p in pos).encode()
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)).encode()
    with open(pfad, "wb") as f:
        f.write(out)


def scan_pdf(pfad):
    from PIL import Image
    Image.new("RGB", (400, 300), "white").save(pfad, "PDF")


def main():
    zustand_neu()
    # ---- 1. off: today's reading, one plain lese_treppe call, nothing changed, no network
    setze()
    aufrufe = []
    erg, ob = ek.beleg_lesen(lambda extra: aufrufe.append(extra) or {"zwilling": "x", "felder": {}}, "t")
    assert aufrufe == [[]] and ob is None
    assert ek.erkennen(dict(ZW)) == (ZW["felder"], None) and ek.gelesen_von(None) == "rules"
    assert not Z["anfragen"] and not Z["koepfe"], "mode off must not touch the network"
    print("ok   off = rules only, no network")

    # ---- 2. field model, local Ollama: not pinned -> refused (rules marked / error)
    setze(erkennung_modus="local", erkennung_modell="fields-7b")
    f, b = ek.erkennen(dict(ZW))
    assert f == ZW["felder"] and b["status"] == ek.REGELN_GENUTZT and b["art"] == "not_pinned", b
    setze(erkennung_modus="local", erkennung_modell="fields-7b", erkennung_fallback="none")
    assert fehler_art(lambda: ek.erkennen(dict(ZW)))[0] == "not_pinned"
    # first successful test pins id + digest
    Z["antwort"] = {"brutto": 1469.06, "netto": 1234.5, "steuer": 234.56, "datum": "2026-01-15"}
    t = ek.testen()
    assert t["ok"] and t["fields"]["pinned_now"] and ek.pin_lesen()["digest"] == "a" * 64, t
    assert all(r[1].get("model") == "fields-7b" for r in Z["anfragen"] if r[0] == "/api/chat")
    print("ok   not pinned -> refused; first successful test pins model id and digest")

    # ---- 3. anti-hallucination: amounts / ids not in the text are dropped and reported; the model fixes the net amount
    Z["antwort"] = {"brutto": "4.998,00", "netto": 4200.0, "steuer": 798.0, "satz": 19, "datum": "2026-02-03", "rechnungsnummer": "RE-77",
                    "ust_id": "DE 123456789", "iban": "DE89370400440532013000", "aussteller": "Phantom AG", "cuit": "30-71234567-1", "waehrung": "EUR"}
    f, b = ek.erkennen(dict(ZW))
    weg = {x["feld"] for x in b["verworfen"]}
    assert weg == {"iban", "aussteller", "cuit"}, b["verworfen"]
    assert f["netto"] == 4200.0 and f["steuer"] == 798.0 and f["ust_id"] == "DE123456789" and "iban" not in f, f
    assert b["status"] == "model" and "netto" in b["felder_modell"] and b["digest"] == "a" * 64, b
    assert vp.verproben(f)["sicher"] and not vp.verproben(ZW["felder"])["bestanden"]
    Z["antwort"] = {"brutto": 999.99, "datum": "2031-01-01"}
    f, b = ek.erkennen(dict(ZW))
    assert "brutto" not in b["felder_modell"] and {x["feld"] for x in b["verworfen"]} == {"brutto", "datum"} and f["brutto"] == 4998.0
    assert ek.datum_im_text("2026-02-03", "am 3. Februar 2026") and ek.datum_im_text("2026-02-03", "20260203") and not ek.datum_im_text("2026-03-04", "03.02.2026")
    assert 4200.0 in ek.zahlen_im_text("Netto 4 200,00") and 1234.5 in ek.zahlen_im_text("1,234.50 USD")
    assert all("keep_alive" in b and b["keep_alive"] == 0 and "images" not in json.dumps(b["messages"]) for p_, b in Z["anfragen"] if p_ == "/api/chat"), \
        "every field-model call sends keep_alive 0 and never page images"
    print("ok   values not in the source text are dropped and reported; model fixes a misread net amount")

    # ---- 4. digest changed / model missing / other model answered -> refused, never replaced
    Z["antwort"] = {"brutto": 4998.0}
    Z["modelle"]["fields-7b"]["digest"] = "b" * 64
    art, text = fehler_art(lambda: ek.erkennen(dict(ZW)))
    assert art == "digest_changed" and "aaaaaaaaaaaa" in text, text
    setze(erkennung_modus="local", erkennung_modell="fields-7b")
    f, b = ek.erkennen(dict(ZW))
    assert b["status"] == ek.REGELN_GENUTZT and b["art"] == "digest_changed" and f == ZW["felder"]
    assert ek.gelesen_von(b, None, "textlayer") == "text: textlayer; fields: rules (field model unavailable)"
    vorher = len([a for a in Z["anfragen"] if a[0] == "/api/chat"])
    ek.erkennen(dict(ZW))
    assert len([a for a in Z["anfragen"] if a[0] == "/api/chat"]) == vorher, "a changed model must not be asked at all"
    Z["modelle"]["fields-7b"]["digest"] = "a" * 64
    del Z["modelle"]["fields-7b"]
    setze(erkennung_modus="local", erkennung_modell="fields-7b", erkennung_fallback="none")
    assert fehler_art(lambda: ek.erkennen(dict(ZW)))[0] == "missing"
    Z["modelle"]["fields-7b:latest"] = {"digest": "a" * 64, "caps": ["completion"]}
    art, text = fehler_art(lambda: ek.erkennen(dict(ZW)))
    assert art == "missing" and "exact id is 'fields-7b:latest'" in text, text  # a near match is named, never used
    del Z["modelle"]["fields-7b:latest"]
    Z["modelle"]["fields-7b"] = {"digest": "a" * 64, "caps": ["completion"]}
    Z["antwort_modell"] = "fields-7b-other"
    assert fehler_art(lambda: ek.erkennen(dict(ZW)))[0] == "other_model"
    Z["antwort_modell"] = None
    assert ek.erkennen(dict(ZW))[1]["status"] in ("model", "rules_kept")  # same pinned version answers again
    print("ok   changed digest, missing model and an answer from another model are refused")

    # ---- 5. OpenAI-compatible endpoint with a key from Hermes
    Z["ollama"] = False
    pins_weg()
    setze(erkennung_modus="local", erkennung_modell="fields-7b")
    Z["antwort"] = {"brutto": 1469.06, "netto": 1234.5, "steuer": 234.56}
    assert ek.testen()["fields"]["pinned_now"] and ek.pin_lesen()["digest"] is None
    Z["antwort"] = {"netto": 4200.0, "steuer": 798.0}
    f, b = ek.erkennen(dict(ZW))
    assert b["status"] == "model" and f["netto"] == 4200.0, b
    assert any(k.get("Authorization") == "Bearer " + SCHLUESSEL for k in Z["koepfe"]), "key from Hermes not sent"
    Z["ollama"] = True
    print("ok   OpenAI-compatible endpoint, key only from Hermes")

    # ---- 6. provider through Hermes (mock): opt-in required, model checked, Trove never sees a key
    pins_weg()
    aufrufe = []

    class Ergebnis:
        def __init__(self, model, parsed, provider="openrouter"):
            self.model, self.parsed, self.text, self.provider = model, parsed, json.dumps(parsed), provider

    class PluginLlm:
        antwort_modell = None
        verbot = False

        def __init__(self, *, plugin_id, **kw):
            assert plugin_id == "reraise-trove"

        def complete_structured(self, **kw):
            aufrufe.append(kw)
            if PluginLlm.verbot:
                raise PermissionError("ZZHERMESTEXT Plugin 'reraise-trove' cannot override the model.")
            return Ergebnis(PluginLlm.antwort_modell or kw["model"], dict(Z["antwort"]))
    fake = types.ModuleType("agent.plugin_llm")
    fake.PluginLlm = PluginLlm
    sys.modules["agent"] = types.ModuleType("agent")
    sys.modules["agent.plugin_llm"] = fake
    setze(erkennung_modus="provider", erkennung_modell="openai/gpt-x", erkennung_provider="openrouter", erkennung_fallback="none")
    assert fehler_art(lambda: ek.erkennen(dict(ZW)))[0] == "not_allowed" and not aufrufe, "provider mode without opt-in must not call"
    setze(erkennung_modus="provider", erkennung_modell="openai/gpt-x", erkennung_provider="openrouter", erkennung_fallback="none", erkennung_extern_erlaubt=True)
    Z["antwort"] = {"brutto": 1469.06, "netto": 1234.5, "steuer": 234.56}
    assert ek.testen()["fields"]["pinned_now"]
    Z["antwort"] = {"netto": 4200.0, "steuer": 798.0, "iban": "DE89370400440532013000"}
    f, b = ek.erkennen(dict(ZW))
    assert f["netto"] == 4200.0 and "iban" not in f and b["modus"] == "provider" and aufrufe[-1]["model"] == "openai/gpt-x" and aufrufe[-1]["provider"] == "openrouter"
    assert not any(k in json.dumps(aufrufe[-1], default=str).lower() for k in ("api_key", "authorization", "bearer"))
    PluginLlm.antwort_modell = "openai/gpt-x-2026-01-01"
    assert fehler_art(lambda: ek.erkennen(dict(ZW)))[0] == "other_model"
    PluginLlm.antwort_modell = None
    PluginLlm.verbot = True
    art, text = fehler_art(lambda: ek.erkennen(dict(ZW)))
    assert art == "not_allowed" and text == ek.HERMES_VERBOT and "ZZHERMESTEXT" not in text  # K5: fixed text
    PluginLlm.verbot = False
    del sys.modules["agent.plugin_llm"], sys.modules["agent"]
    assert fehler_art(lambda: ek.erkennen(dict(ZW)))[0] == "no_hermes"
    print("ok   provider mode: opt-in, Hermes facade, exact model, trust refusal passed on")

    # ---- 7. OCR stage: pin = digest + layers + GGUF files; any change refused
    pins_weg()
    g1, g2 = os.path.join(TMP, "m.gguf"), os.path.join(TMP, "mmproj.gguf")
    for p, inhalt in ((g1, b"weights-1"), (g2, b"projector-1")):
        with open(p, "wb") as f:
            f.write(inhalt)
    import hashlib
    h1 = hashlib.sha256(b"weights-1").hexdigest()
    setze(erkennung_ocr_modell="ocr-3b", erkennung_ocr_dateien=g1 + "," + g2, erkennung_fallback="none")
    Z["ocr_text"] = "title [1, 2, 3, 4]ACME Supplies GmbH\ntext [5, 6, 7, 8]Total 1.469,06 EUR\ntext [1, 1, 1, 1]Net amount 1.234,50\nVAT 19 % 234,56\n" + "Barzahlung\nDeutsche Post\n" * 40 + "Signatur " + "1." * 400 + "\nLAST LINE\n"
    t = ek.testen()
    pin = konfig.lesen()[ek.PIN_OCR]
    assert t["ok"] and t["ocr"]["pinned_now"] and pin["digest"] == "c" * 64 and pin["schichten"] == ["1" * 64, "2" * 64] and pin["dateien"]["m.gguf"] == h1, (t, pin)
    assert "[1, 2, 3, 4]" not in t["ocr"]["text"] and t["ocr"]["text"].count("Barzahlung") == 1
    assert "1.1.1.1.1.1.1.1.1" not in ek.ocr_text_saeubern("Signatur " + "1." * 400) and ek._wiederholt([], "x" + "1." * 30)
    ek.ocr_aufloesen(ek.konfiguration())
    with open(g1, "ab") as f:
        f.write(b"x")
    art, text = fehler_art(lambda: ek.ocr_aufloesen(ek.konfiguration()))
    assert art == "digest_changed" and "GGUF" in text, text
    with open(g1, "wb") as f:
        f.write(b"weights-1")
    ek.ocr_aufloesen(ek.konfiguration())
    Z["schichten"]["ocr-3b"] = ["1" * 64, "3" * 64]
    assert "layers" in fehler_art(lambda: ek.ocr_aufloesen(ek.konfiguration()))[1]
    Z["schichten"]["ocr-3b"] = ["1" * 64, "2" * 64]
    setze(erkennung_ocr_modell="ocr-3b", erkennung_ocr_dateien=g1 + "," + g2, erkennung_fallback="none", erkennung_ocr_sha256="f" * 64)
    assert fehler_art(lambda: ek.ocr_aufloesen(ek.konfiguration()))[0] == "digest_changed"
    setze(erkennung_ocr_modell="ocr-3b", erkennung_ocr_dateien=g1 + "," + g2, erkennung_fallback="none", erkennung_ocr_sha256=h1 + "," + hashlib.sha256(b"projector-1").hexdigest())
    ek.ocr_aufloesen(ek.konfiguration())
    Z["modelle"]["fields-7b"]["caps"] = ["completion"]
    setze(erkennung_ocr_modell="fields-7b", erkennung_fallback="none")
    assert "vision" in fehler_art(lambda: ek.ocr_aufloesen(ek.konfiguration(), pin_pruefen=False))[1]
    print("ok   OCR stage pin: digest, layers and GGUF sha256; every change refused")

    # ---- 8. OCR stage end to end through the backend route (real lese_treppe subprocess): scan and second reading
    import plugin_api as pa
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(pa.router, prefix="/p")
    c = TestClient(app, base_url="http://127.0.0.1")
    ein = os.path.join(TMP, "ein")
    os.makedirs(ein)
    scan_pdf(os.path.join(ein, "scan.pdf"))
    text_pdf(os.path.join(ein, "text.pdf"), ["ACME GmbH Rechnung Nr. RE-77 Datum 03.02.2026", "Netto 12,34 EUR Zwischenstand Lieferung",
                                              "Gesamtbetrag 4.998,00 EUR inkl. MwSt 19 % 798,00 EUR", "Bitte ueberweisen Sie den Betrag innerhalb von 14 Tagen."])
    q = c.post("/p/quellen", json={"mandant": "t", "art": "ordner", "pfad": ein, "nur_lesen": True}).json()
    qid = (q.get("quelle") or q).get("id")
    pins_weg()
    setze(erkennung_ocr_modell="ocr-3b", erkennung_ocr_dateien=g1, erkennung_fallback="none")
    Z["ocr_text"] = "Total 1.469,06 EUR\nNet amount 1.234,50\nVAT 19 % 234,56\n"
    assert ek.testen()["ok"]
    Z["ocr_text"] = "text [1, 2, 3, 4]Kasse 7\nDatum 14.03.2026\nNetto 20,00 EUR\nMwSt 19 % 3,80 EUR\nSumme 23,80 EUR\n"
    r = c.post("/p/lesen", json={"mandant": "t", "quelle": qid, "pfad": "scan.pdf"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["methode"] == "ocr:modell" and d["felder"]["brutto"] == 23.8 and d["verprobung"]["bestanden"], d
    assert d["read_by"] == "text: ocr model ocr-3b@cccccccccccc; fields: rules" and d["ocr_modell"]["status"] == "ocr", d["read_by"]
    z = json.load(open(d["zwilling"], encoding="utf-8"))
    assert z["ocr_modell"]["digest"] == "c" * 64 and "seitenbilder" not in z and "_seitenbilder" not in z
    proto = open(os.path.join(os.environ["TROVE_DATEN"], "t", "belegprotokoll.jsonl"), encoding="utf-8").read() if os.path.exists(os.path.join(os.environ["TROVE_DATEN"], "t", "belegprotokoll.jsonl")) else ""
    # text PDF: the first reading fails (net 12.34), the OCR model reading passes and is used
    setze(erkennung_ocr_modell="ocr-3b", erkennung_ocr_dateien=g1, erkennung_ocr_einsatz="second_reading", erkennung_fallback="none")
    Z["ocr_text"] = "Rechnung RE-77 Datum 03.02.2026\nNetto 4.200,00 EUR\nMwSt 19 % 798,00 EUR\nGesamtbetrag 4.998,00 EUR\n"
    d = c.post("/p/lesen", json={"mandant": "t", "quelle": qid, "pfad": "text.pdf"}).json()
    assert d["methode"] == "textlayer" and d["ocr_modell"]["status"] == "second_reading_used" and d["felder"]["netto"] == 4200.0 and d["verprobung"]["sicher"], d
    # both readings, gross differs -> R7 fails
    Z["ocr_text"] = "Gesamtbetrag 5.842,13 EUR\nDatum 03.02.2026\n"
    d = c.post("/p/lesen", json={"mandant": "t", "quelle": qid, "pfad": "text.pdf"}).json()
    assert d["verprobung"]["regeln"]["R7_zwei_lesungen"] is False and not d["verprobung"]["bestanden"], d
    # unavailable: fallback none -> 503, fallback rules -> plain reading, marked
    Z["modelle"]["ocr-3b"]["digest"] = "d" * 64
    r = c.post("/p/lesen", json={"mandant": "t", "quelle": qid, "pfad": "text.pdf"})
    assert r.status_code == 503 and "changed" in r.json()["detail"], r.text
    setze(erkennung_ocr_modell="ocr-3b", erkennung_ocr_dateien=g1, erkennung_ocr_einsatz="second_reading", erkennung_fallback="rules")
    d = c.post("/p/lesen", json={"mandant": "t", "quelle": qid, "pfad": "text.pdf"}).json()
    assert d["ocr_modell"]["status"] == ek.REGELN_GENUTZT and d["read_by"] == "text: textlayer (ocr model unavailable); fields: rules" and d["verprobung"]["regeln"]["R7_zwei_lesungen"] is None, d
    Z["modelle"]["ocr-3b"]["digest"] = "c" * 64
    # off again: exactly the old answer plus read_by "rules"
    setze()
    d = c.post("/p/lesen", json={"mandant": "t", "quelle": qid, "pfad": "text.pdf"}).json()
    assert d["read_by"] == "rules" and "ocr_modell" not in d and "erkennung" not in d and d["felder"]["netto"] == 12.34, d
    st = c.get("/p/erkennung").json()
    assert st["mode"] == "off" and st["ocr"] is None
    assert c.post("/p/erkennung/pin", json={}).status_code == 400
    print("ok   backend: OCR model reads scans, second reading with R7, refusal and marked fallback, off unchanged")

    # ---- 8b. K4: OCR failure with fallback none -> no half-written twin, a "fehler" line in the receipt log
    pins_weg()
    setze(erkennung_ocr_modell="ocr-3b", erkennung_ocr_dateien=g1, erkennung_fallback="none")
    Z["ocr_text"] = "Total 1.469,06 EUR\nNet amount 1.234,50\nVAT 19 % 234,56\n"
    assert ek.testen()["ok"]
    zw_dir = os.path.join(os.environ["TROVE_DATEN"], "t", "zwillinge")
    vorher = set(os.listdir(zw_dir))
    Z["antwort_modell"] = "someone-else"
    r = c.post("/p/lesen", json={"mandant": "t", "quelle": qid, "pfad": "scan.pdf"})
    Z["antwort_modell"] = None
    assert r.status_code == 503 and "someone-else" in r.json()["detail"], r.text
    neu_tw = set(os.listdir(zw_dir)) - vorher
    assert not [n for n in neu_tw if n.endswith(".json")] , neu_tw
    zw = [json.load(open(os.path.join(zw_dir, n), encoding="utf-8")) for n in os.listdir(zw_dir) if n.endswith(".json")]
    assert not any(x.get("methode") == "ocr:ausstehend" for x in zw), "a pending twin was left behind"
    import belegprotokoll as bp
    letzte = list(bp.eintraege(bp.pfad_fuer("t")))[-1]
    assert letzte["ereignis"] == "fehler" and "recognition model" in letzte["meldung"], letzte
    print("ok   K4: failed model read with fallback none leaves no half twin and logs a fehler line")
    pruefung_teil()

    # ---- 9. MCP server: same configured model (settings from Hermes' config.yaml), no tool changes it
    try:
        import mcp  # noqa: F401
        mcp_teil(qid, g1)
    except ImportError:
        print("skip MCP part: the 'mcp' package is not installed")

    # ---- 10. the key never appears anywhere
    funde = []
    for w, _, fs in os.walk(os.environ["TROVE_DATEN"]):
        for n in fs:
            if SCHLUESSEL in open(os.path.join(w, n), encoding="utf-8", errors="replace").read():
                funde.append(n)
    assert not funde, funde
    assert not any(SCHLUESSEL in m or "ZZBODYMARKZZ" in m for m in LOGS), "key or upstream body in a log line"
    print("ok   the endpoint key is in no file, no log line, no response")
    print("erkennung ok")


def pruefung_teil():
    """Findings of the independent review (B1, B2, S1-S4, K1-K5, keep_alive)."""
    import verprobung as vp
    import belegprotokoll as bp
    pins_weg()
    Z["antwort_modell"] = None
    setze(erkennung_modus="local", erkennung_modell="fields-7b", erkennung_fallback="rules")
    Z["antwort"] = {"brutto": 1469.06, "netto": 1234.5, "steuer": 234.56}
    assert ek.testen()["ok"]
    # B1: factor-100, dates, IDs are not amounts; one number format per document
    txt = "Muster Handel GmbH\nRechnung Nr. RE-2026-77 / 4711\nDatum 15.01.2026 18:47\nNetto 10,50 EUR\nMwSt 19% 2,00 EUR\nGesamtbetrag 12,50 EUR\n"
    zahlen = ek.zahlen_im_text(txt)
    assert zahlen == {10.5, 2.0, 12.5}, zahlen
    for falsch in (1250.0, 1050.0, 200.0, 2026.0, 15.01, 1501.0, 4711.0, 18.47, 19.0):
        assert falsch not in zahlen, falsch
    assert ek.zahlen_im_text("Total 1,234.56 USD, shipping 12.50") == {1234.56, 12.5}
    assert ek.zahlen_im_text("Summe 1.234,56 EUR") == {1234.56} and 1.23 not in ek.zahlen_im_text("Summe 1.234,56 EUR")
    assert ek.zahlen_im_text("Betrag 1.23,45") == set(), "thousands groups must have exactly three digits"
    assert ek.zahlen_im_text("Gesamt 180 EUR, Pos. 3 Stueck 25") == {180.0} and ek.zahlen_im_text("Preis 49,- netto") == {49.0}
    assert ek.zahlformat("Netto 1.234,56 Brutto 1.469,13") == ("de",) and 12.5 not in ek.zahlen_im_text("Netto 1.234,56 Brutto 1.469,13 Menge 12.50")
    z = {"methode": "textlayer", "text": txt, "felder": {}}
    Z["antwort"] = {"netto": 1050, "steuer": 200, "brutto": 1250, "datum": "2026-01-15", "rechnungsnummer": "RE-2026-77", "aussteller": "Muster Handel GmbH"}
    f, b = ek.erkennen(dict(z))
    assert "brutto" not in f and {x["feld"] for x in b["verworfen"]} >= {"brutto", "netto", "steuer"} and not vp.verproben(f)["bestanden"], (f, b)
    # B1: rule reading and model both pass but disagree -> Widerspruch Regeln/Modell, not passed, both values shown
    txt2 = "Rechnung RE-5 Datum 03.02.2026\nNetto 100,00 MwSt 19,00 Gesamt 119,00 EUR\nNetto 200,00 MwSt 38,00 Gesamt 238,00 EUR\n"
    regeln = {"datum": "2026-02-03", "brutto": 238.0, "netto": 200.0, "steuer": 38.0}
    assert vp.verproben(regeln)["bestanden"]
    Z["antwort"] = {"brutto": 119.0, "netto": 100.0, "steuer": 19.0}
    f, b = ek.erkennen({"methode": "textlayer", "text": txt2, "felder": dict(regeln)})
    assert b["status"] == "conflict" and f == regeln and b["widerspruch"]["brutto"] == {"regeln": 238.0, "modell": 119.0}, b
    v = vp.verproben(f, None, b["widerspruch"])
    assert not v["bestanden"] and v["regeln"]["R8_regeln_modell"] is False and "Widerspruch Regeln/Modell" in v["gruende"][0] and "238.0" in v["gruende"][0]
    assert ek.WIDERSPRUCH in ek.gelesen_von(b, None, "textlayer")
    assert "R8_regeln_modell" not in vp.verproben(regeln)["regeln"], "without a field model the rule set is unchanged"
    # a differing proposal against a passed rule reading is a Widerspruch even if the proposal itself fails the rules
    Z["antwort"] = {"brutto": 119.0, "netto": 200.0}
    f, b = ek.erkennen({"methode": "textlayer", "text": txt2, "felder": dict(regeln)})
    assert b["status"] == "conflict" and f == regeln, b
    # a proposal that fails the rules never replaces a failed rule reading either
    schlecht = {"datum": "2026-02-03", "brutto": 238.0, "netto": 20.0}
    Z["antwort"] = {"steuer": 19.0}
    f, b = ek.erkennen({"methode": "textlayer", "text": txt2, "felder": dict(schlecht)})
    assert b["status"] == "rules_kept" and f == schlecht, b
    print("ok   B1: one number format per document, no amounts from dates/IDs, factor 100 dropped, Widerspruch Regeln/Modell does not pass")

    # B2: field model reads text only; read_by names the source of every field
    n_vorher = len(Z["anfragen"])
    Z["antwort"] = {"netto": 4200.0, "steuer": 798.0}
    f, b = ek.erkennen({"methode": "ocr:rapidocr", "text": TEXT, "felder": dict(ZW["felder"])})
    gesendet = [bd for p_, bd in Z["anfragen"][n_vorher:] if p_ == "/api/chat"]
    assert gesendet and all("images" not in json.dumps(bd) for bd in gesendet)
    rb = ek.gelesen_von(b, None, "ocr:rapidocr")
    assert rb == "text: ocr:rapidocr; fields: model local:fields-7b@aaaaaaaaaaaa [netto, steuer]; rules [brutto, datum, waehrung]", rb
    import inspect
    assert "bilder" not in inspect.signature(ek.erkennen).parameters and "erkennung_bilder" not in zugang.STANDARD
    print("ok   B2: no images to the field model; read_by names text source and field origin")

    # S1: local = this machine or the private network only
    for ep in ("http://127.0.0.1:11434", "http://[::1]:11434", "http://10.1.2.3", "https://172.20.0.5:8443", "http://192.168.0.10:11434", "http://localhost:11434"):
        ek.endpoint_pruefen(ep)
    for ep in ("http://169.254.169.254", "HTTP://evil.example.com", "http://8.8.8.8", "http://172.32.0.1", "http://[2001:db8::1]", "http://100.64.0.1",
               "http://user:pw@127.0.0.1", "ftp://127.0.0.1", "http://127.0.0.1:11434@evil"):
        try:
            ek.endpoint_pruefen(ep)
            raise AssertionError("accepted " + ep)
        except ek.ErkennungFehler as ex:
            assert ex.art in ("not_local", "config"), (ep, ex.art)
    setze(erkennung_modus="local", erkennung_modell="fields-7b", erkennung_fallback="none", erkennung_endpoint="http://8.8.8.8:11434")
    assert fehler_art(lambda: ek.erkennen(dict(ZW)))[0] == "not_local"
    setze(erkennung_ocr_modell="ocr-3b", erkennung_fallback="none", erkennung_endpoint="http://8.8.8.8:11434")
    assert fehler_art(lambda: ek.ocr_aufloesen(ek.konfiguration(), pin_pruefen=False))[0] == "not_local"
    print("ok   S1: only loopback / private network endpoints")

    # S2: redirects are not followed, the key never reaches another host (real sockets)
    erhalten = []

    class Ziel(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            erhalten.append(self.headers.get("Authorization"))
            self.send_response(404)
            self.end_headers()
    ziel = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Ziel)
    threading.Thread(target=ziel.serve_forever, daemon=True).start()

    class Umleiter(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://localhost:%d/steal" % ziel.server_address[1])
            self.end_headers()
        do_POST = do_GET
    um = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Umleiter)
    threading.Thread(target=um.serve_forever, daemon=True).start()
    setze(erkennung_modus="local", erkennung_modell="fields-7b", erkennung_fallback="none", erkennung_endpoint="http://127.0.0.1:%d" % um.server_address[1])
    art, text = fehler_art(lambda: ek.erkennen(dict(ZW)))
    assert art == "bad_answer" and "redirect" in text and erhalten == [], (art, text, erhalten)
    um.shutdown()
    ziel.shutdown()
    print("ok   S2: no redirect is followed, the key stays with the endpoint")

    # S4: every answer must name exactly the pinned model (Ollama field, OpenAI-compatible, OCR stream)
    setze(erkennung_modus="local", erkennung_modell="fields-7b", erkennung_fallback="none")
    for name in ("", "fields-7b:other"):
        Z["antwort_modell"] = name or " "
        art, text = fehler_art(lambda: ek.erkennen(dict(ZW)))
        assert art == "other_model", (name, art, text)
    Z["ollama"] = False
    pins_weg()
    Z["antwort_modell"] = None
    Z["antwort"] = {"brutto": 1469.06, "netto": 1234.5, "steuer": 234.56}
    assert ek.testen()["ok"]
    Z["antwort_modell"] = " "
    assert fehler_art(lambda: ek.erkennen(dict(ZW)))[0] == "other_model"
    Z["ollama"], Z["antwort_modell"] = True, None
    k = dict(ek.konfiguration(), ocr_modell="ocr-3b")
    Z["ocr_text"], Z["antwort_modell"] = "Total 12,50 EUR\n", " "
    assert fehler_art(lambda: ek.ocr_seite(k, "aGk="))[0] == "other_model"
    Z["antwort_modell"] = None
    print("ok   S4: an answer without the exact model name is refused")

    # K1: GGUF hash only cached within the process; a changed file is noticed even with the same size and time
    g = os.path.join(TMP, "k1.gguf")
    with open(g, "wb") as fh:
        fh.write(b"A" * 100)
    h1 = ek.datei_sha256(g)
    st = os.stat(g)
    with open(g, "wb") as fh:
        fh.write(b"B" * 100)
    os.utime(g, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert ek.datei_sha256(g) != h1 and ek.datei_sha256(g, neu=True) != h1
    assert not os.path.exists(os.path.join(os.environ["TROVE_DATEN"], "erkennung_hashes.json"))
    # K2: identifiers need a minimum length and must stand as a whole token
    w, weg = ek.normalisieren({"rechnungsnummer": "1", "aussteller": "DE", "iban": "DE8937", "ust_id": "DE12345", "cuit": "20-123"})
    assert not w and len(weg) == 5, (w, weg)
    t = "Rechnung RE-2026-0417 / Kunde 12345\nIBAN DE89 3704 0044 0532 0130 00\nACME Supplies GmbH"
    assert ek.im_text("rechnungsnummer", "RE-2026-0417", t, set()) and not ek.im_text("rechnungsnummer", "2026", t, set())
    assert not ek.im_text("rechnungsnummer", "234", t, set()) and ek.im_text("iban", "DE89370400440532013000", t, set())
    assert ek.im_text("aussteller", "ACME Supplies GmbH", t, set()) and not ek.im_text("aussteller", "ACM", t, set())
    # K3: YAML booleans in word settings
    setze(erkennung_modus=False, erkennung_fallback="rules", erkennung_ocr_einsatz=None)
    kk = ek.konfiguration()
    assert kk["modus"] == "off" and kk["ocr_einsatz"] == "scans" and kk["halten"] == "0"
    assert zugang._skalar("off") == "off" and zugang._skalar("false") is False and ek._wort(False, "auto") == "off"
    print("ok   K1-K3: in-process hash cache only, identifiers as whole tokens, YAML booleans")

    # keep_alive: every Ollama call; OCR keeps the model between pages, unloads after the last (default 0)
    setze(erkennung_ocr_modell="ocr-3b", erkennung_fallback="none")
    n_vorher = len(Z["anfragen"])
    Z["ocr_text"] = "Total 12,50 EUR\n"
    ek.ocr_lesen(ek.konfiguration(), ["aGk=", "aGk=", "aGk="])
    ka = [bd.get("keep_alive") for p_, bd in Z["anfragen"][n_vorher:] if p_ == "/api/chat"]
    assert ka == [60, 60, 0], ka
    setze(erkennung_ocr_modell="ocr-3b", erkennung_fallback="none", erkennung_halten="10m")
    n_vorher = len(Z["anfragen"])
    ek.ocr_lesen(ek.konfiguration(), ["aGk="])
    assert [bd.get("keep_alive") for p_, bd in Z["anfragen"][n_vorher:] if p_ == "/api/chat"] == ["10m"]
    setze(erkennung_ocr_modell="ocr-3b", erkennung_fallback="none", erkennung_halten="bald")
    assert fehler_art(lambda: ek.ocr_lesen(ek.konfiguration(), ["aGk="]))[0] == "config"
    print("ok   keep_alive on every Ollama call (default 0 = unload after the read)")
    pruefung_v2()


def pruefung_v2():
    """Second review (S-v2-1, K-v2-1..5)."""
    import verprobung as vp
    import socket as _s
    pins_weg()
    Z["antwort_modell"] = None
    setze(erkennung_modus="local", erkennung_modell="fields-7b", erkennung_fallback="rules")
    Z["antwort"] = {"brutto": 1469.06, "netto": 1234.5, "steuer": 234.56}
    assert ek.testen()["ok"]
    # S-v2-1: every field, not only amounts
    txt = ("Muster GmbH\nRechnung Nr. RE-2026-77\nRechnungsdatum 15.01.2026\nLieferdatum 10.01.2026\nNetto 100,00 EUR\nMwSt 19% 19,00 EUR\n"
           "Brutto 119,00 EUR\nAuftrag RE-2026-78 USD\n")
    regeln = {"datum": "2026-01-15", "brutto": 119.0, "netto": 100.0, "steuer": 19.0, "rechnungsnummer": "RE-2026-77", "waehrung": "EUR"}
    assert vp.verproben(regeln)["bestanden"]
    for vorschlag, feld in (({"datum": "2026-01-10"}, "datum"), ({"rechnungsnummer": "RE-2026-78"}, "rechnungsnummer"),
                            ({"waehrung": "USD"}, "waehrung"), ({"brutto": 119.0, "netto": 100.0, "steuer": 19.0, "datum": "2026-01-10"}, "datum")):
        Z["antwort"] = vorschlag
        f, b = ek.erkennen({"methode": "textlayer", "text": txt, "felder": dict(regeln)})
        v = vp.verproben(f, None, b["widerspruch"])
        assert b["status"] == "conflict" and f == regeln and set(b["widerspruch"]) == {feld} and not v["bestanden"], (vorschlag, b)
        assert "Widerspruch Regeln/Modell" in " ".join(v["gruende"]) and str(regeln[feld]) in " ".join(v["gruende"])
    Z["antwort"] = {"datum": "2026-01-15", "rechnungsnummer": "re-2026-77", "brutto": 119.0}  # same values: no conflict
    f, b = ek.erkennen({"methode": "textlayer", "text": txt, "felder": dict(regeln)})
    assert b["status"] == "model" and not b["widerspruch"] and b["felder_modell"] == [], b
    # K-v2-5: all model values dropped -> status rules, read_by says so
    Z["antwort"] = {"brutto": 999.99}
    f, b = ek.erkennen({"methode": "textlayer", "text": txt, "felder": dict(regeln)})
    assert b["status"] == "rules" and ek.gelesen_von(b, None, "textlayer") == "text: textlayer; fields: rules (model values dropped)", b
    print("ok   S-v2-1/K-v2-5: any field differing from a passed rule reading is a Widerspruch; dropped proposals say rules")
    # K-v2-1: ambiguous format -> no amounts; date without year is no amount
    assert ek.zahlformat("A 1.234,56 EUR\nB 1,234.56 USD") == () and ek.zahlen_im_text("A 1.234,56 EUR\nB 1,234.56 USD") == set()
    assert 1659.0 not in ek.zahlen_im_text("Summe 66.36 EUR\nPreis 1,659 EUR/l\nNetto 55,76")
    assert 15.01 not in ek.zahlen_im_text("Total 3.50\nDelivered 15.01.\nTax 0.50") and 3.5 in ek.zahlen_im_text("Total 3.50\nDelivered 15.01.\nTax 0.50")
    # K-v2-2: whole numbers only with a directly attached currency, not after counters, not year-like
    for t, falsch in (("Pos. 3 EUR 12,50", 3.0), ("Kontonummer 2026 EUR-Konto\nSumme 12,50 EUR", 2026.0), ("Pos 3 Summe 12,50 EUR", 3.0),
                      ("Jahr 2026 EUR 12,50", 2026.0), ("Art.-Nr. 4711,00\nSumme 12,50 EUR", 4711.0), ("Menge 40 EUR/l 1,65", 40.0)):
        assert falsch not in ek.zahlen_im_text(t), (t, ek.zahlen_im_text(t))
    for t, gut in (("Gesamt 180 EUR", 180.0), ("Preis 49,- netto", 49.0), ("Total €12", 12.0), ("Summe 12€", 12.0)):
        assert gut in ek.zahlen_im_text(t), (t, ek.zahlen_im_text(t))
    print("ok   K-v2-1/2: ambiguous number format gives no amounts; whole numbers only with an attached currency")
    # K-v2-3: every stream chunk is checked
    k = dict(ek.konfiguration(), ocr_modell="ocr-3b")
    alt = ek.http_quelle
    for teile, ok in (([{"model": "ocr-3b", "message": {"content": "a\n"}}, {"model": "evil", "message": {"content": "b"}, "done": True}], False),
                      ([{"message": {"content": "a\n"}}, {"model": "ocr-3b", "message": {"content": "b"}, "done": True}], True),
                      ([{"message": {"content": "a"}, "done": True}], False)):
        ek.http_quelle = lambda *a, _t=teile: (200, list(_t))
        try:
            ek.ocr_seite(k, "aGk=")
            assert ok, teile
        except ek.ErkennungFehler as ex:
            assert not ok and ex.art == "other_model", teile
    ek.http_quelle = alt
    # K-v2-4: the name is resolved once; the connection goes to exactly the validated IP, Host header keeps the name
    aufrufe = []
    echt = _s.getaddrinfo

    def gai(host, *a, **kw):
        if host == "modell.local":
            aufrufe.append(host)
            ip = "127.0.0.1" if len(aufrufe) == 1 else "203.0.113.7"
            return [(_s.AF_INET, _s.SOCK_STREAM, 6, "", (ip, a[0] if a and a[0] else 0))]
        return echt(host, *a, **kw)
    _s.getaddrinfo = gai
    ek.http_quelle = None
    try:
        port = SERVER.server_address[1]
        setze(erkennung_modus="local", erkennung_modell="fields-7b", erkennung_fallback="none", erkennung_endpoint="http://modell.local:%d" % port)
        n = len(Z["koepfe"])
        st, d = ek._http("GET", "http://modell.local:%d/api/tags" % (port,)) if ek.endpoint_pruefen("http://modell.local:%d" % port) else (0, None)
        st2, _ = ek._http("GET", "http://modell.local:%d/api/tags" % (port,))
        assert st == 200 and st2 == 200 and len(aufrufe) == 1, (st, st2, aufrufe)
        assert all(h.get("Host") == "modell.local:%d" % port for h in Z["koepfe"][n:]), Z["koepfe"][n:]
    finally:
        _s.getaddrinfo = echt
        ek.http_quelle = alt
        ek._AUFGELOEST.clear()
    print("ok   K-v2-3/4: model name checked on every chunk; name resolved once, connection to the validated IP")


def mcp_teil(qid, g1):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    with open(os.path.join(os.environ["HERMES_HOME"], "config.yaml"), "w", encoding="utf-8") as f:
        f.write("model:\n  default: x\nplugins:\n  enabled:\n    - reraise-trove\n  entries:\n    reraise-trove:\n      settings:\n"
                "        erkennung_ocr_modell: ocr-3b\n        erkennung_ocr_einsatz: second_reading\n        erkennung_endpoint: \"%s\"  # fake\n"
                "        erkennung_fallback: none\n        erkennung_modus: off\n        erkennung_ocr_dateien: '%s'\n" % (EP, g1))
    pins_weg()
    setze(erkennung_ocr_modell="ocr-3b", erkennung_ocr_einsatz="second_reading", erkennung_fallback="none", erkennung_ocr_dateien=g1)
    Z["ocr_text"] = "Total 1.469,06 EUR\nNet amount 1.234,50\nVAT 19 % 234,56\n"
    assert ek.testen()["ok"]  # the human pins in the tab; the MCP server only uses the pin
    Z["ocr_text"] = "Rechnung RE-77 Datum 03.02.2026\nNetto 4.200,00 EUR\nMwSt 19 % 798,00 EUR\nGesamtbetrag 4.998,00 EUR\n"
    env = dict(os.environ, PYTHONPATH="")

    async def lauf():
        params = StdioServerParameters(command=sys.executable, args=[os.path.join(DASH, "mcp_server.py")], env=env)
        async with stdio_client(params) as (rd, wr):
            async with ClientSession(rd, wr) as s:
                await s.initialize()
                namen = [t.name for t in (await s.list_tools()).tools]
                assert not [n for n in namen if any(w in n.lower() for w in ("pin", "model", "erkennung", "setting", "config"))], namen
                r = await s.call_tool("read_receipt", {"source": qid, "path": "text.pdf"})
                roh = "\n".join(getattr(x, "text", "") for x in r.content)
                assert not getattr(r, "isError", False), roh
                return json.loads(roh)
    d = asyncio.run(lauf())
    assert d.get("read_by", "") == "text: textlayer, replaced by second reading of ocr model ocr-3b@cccccccccccc; fields: rules" \
        and d["ocr_modell"]["status"] == "second_reading_used", d
    print("ok   MCP read_receipt uses the configured, pinned model and shows read_by; no MCP tool changes the model")


if __name__ == "__main__":
    try:
        main()
    finally:
        SERVER.shutdown()
        shutil.rmtree(TMP, ignore_errors=True)
