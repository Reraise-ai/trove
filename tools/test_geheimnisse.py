#!/usr/bin/env python3
"""Leak test for the optional integrations (after the independent review, findings A2-A4, K3).

Secrets come from a Hermes mock. Echo servers on 127.0.0.1 send the secrets back in error bodies (also transformed: upper case,
base64, hex, HTML entities, a line break in the middle), in reason phrases, at the truncation boundary and in redirect targets; the
Gmail mock echoes the client id. Every upstream body carries the marker ZZBODYMARKZZ: it must never appear anywhere (bodies are not
passed on at all, N1). A receipt-log line written before any secret was read must reach the ERPNext mirror redacted (N3). Three secret shapes: plain, with backslash and quote, and multi-line.
Afterwards every HTTP response, every Python log line and every file in the data folder (receipt log included) is scanned for each
secret in every spelling and for every 6-character piece of it. Nothing may be found. No real service is contacted.
"""
import base64
import html
import http.server
import imaplib
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import warnings
from urllib.parse import quote, quote_plus

warnings.filterwarnings("ignore")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORMEN = {
    "plain": lambda t: "Qz7%sk8Hw2Lp9Xv4Tn6" % t,
    "quote": lambda t: 'Qz7%sk8\\Hw2"Lp9/Xv+4 Tn6' % t,
    "multiline": lambda t: "Qz7%sk8Hw2Lp9\nXv4Tn6Ru5Ym3" % t,
}
NAMEN = {"TROVE_DOCUSEAL_API_KEY": "DSa", "TROVE_ERPNEXT_API_KEY": "EKb", "TROVE_ERPNEXT_API_SECRET": "ESc", "TROVE_IMAP_PASSWORD": "IMd",
         "TROVE_RERAISE_KEY": "RRe", "TROVE_GMAIL_CLIENT_ID": "GIf"}


def verwandelt(t):
    """What a server might do to a secret before echoing it: upper case, HTML entities, base64, hex, a line break in the middle."""
    b = t.encode("utf-8")
    return " | ".join([t.upper(), html.escape(t).replace("-", "&#45;"), base64.b64encode(b).decode(), base64.urlsafe_b64encode(b).decode(),
                       b.hex(), t[:5] + "\n" + t[5:]])


def verwandelt_alle(t):
    b = t.encode("utf-8")
    return [x for x in (t.upper(), base64.b64encode(b).decode(), b.hex()) if len(x) >= 8]


def kalter_spiegel(form):
    """N3: a receipt-log line that already contains a secret (written before this process read any) must reach ERPNext redacted."""
    tmp = tempfile.mkdtemp(prefix="trove-cold-")
    try:
        dash = os.path.join(REPO, "plugin", "dashboard")
        code = r"""
import json, os, sys, threading, http.server
sys.path.insert(0, %r)
os.environ["TROVE_DATEN"] = %r
import belegprotokoll as bp
geheim = %r
bp.schreiben(bp.pfad_fuer("haus"), ereignis="notiz", meldung="alte Zeile mit " + geheim)  # zugang not loaded yet: nothing known
import zugang, spiegel_erpnext as se
assert not zugang._bekannt
got = []
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); got.append(self.rfile.read(n).decode())
        b = b'{"message": []}'; self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    do_GET = do_PUT = do_POST
srv = http.server.HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
zugang.geheimnis_quelle = {"TROVE_ERPNEXT_API_KEY": "ek-cold-77", "TROVE_ERPNEXT_API_SECRET": "es-cold-88", "TROVE_RERAISE_KEY": geheim}.get
zugang.einstellungen_quelle = lambda: {"erpnext_url": "http://127.0.0.1:%%d" %% srv.server_address[1]}
se.spiegeln("haus")
srv.shutdown()
alles = "\n".join(got)
print("OK" if got and geheim not in alles and json.dumps(geheim)[1:-1] not in alles else "LEAK " + alles[:300])
""" % (dash, os.path.join(tmp, "data"), FORMEN[form]("RRz"))
        import subprocess
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        ok = r.returncode == 0 and r.stdout.strip().endswith("OK")
        print(("PASS " if ok else "FAIL ") + "%s: cold-process ERPNext mirror redacts an old receipt-log line" % form)
        if not ok:
            print((r.stdout + r.stderr)[-800:])
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def lauf(form):
    tmp = tempfile.mkdtemp(prefix="trove-leak-")
    try:
        plugin = os.path.join(tmp, "plugins", "reraise-trove")
        shutil.copytree(os.path.join(REPO, "plugin"), plugin, ignore=shutil.ignore_patterns("__pycache__"))
        os.environ["TROVE_DATEN"] = os.path.join(tmp, "data")
        os.environ["HOME"] = os.environ["USERPROFILE"] = os.path.join(tmp, "home")
        os.makedirs(os.environ["HOME"])
        os.environ.pop("TROVE_KONFIG", None)
        for m in [m for m in sys.modules if m in ("zugang", "konfig", "belegprotokoll", "docuseal", "spiegel_erpnext", "mail_abholer",
                                                    "reraise_hochladen", "gemeinsam", "oeffentlich", "wissenskarte", "aenderungen", "gesundheit")]:
            del sys.modules[m]
        sys.path[:] = [p for p in sys.path if not p.endswith(os.path.join("reraise-trove", "dashboard"))]
        log = []

        class Fang(logging.Handler):
            def emit(self, r):
                log.append(r.getMessage() + " " + (r.exc_text or ""))
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        fang = Fang()
        root.addHandler(fang)
        spec = importlib.util.spec_from_file_location("trove_leak_%s" % form, os.path.join(plugin, "dashboard", "plugin_api.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(mod.router, prefix="/p")
        c = TestClient(app, raise_server_exceptions=False)
        import zugang
        import reraise_hochladen as rh
        geheim = {n: FORMEN[form](t) for n, t in NAMEN.items()}
        zugang.geheimnis_quelle = geheim.get
        antworten = []
        empfangen = []
        modus = {"m": "ok"}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def antwort(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                empfangen.append(body.decode("utf-8", "replace"))
                tok = self.headers.get("X-Auth-Token") or (self.headers.get("Authorization") or "").split(":")[-1]
                m, p = modus["m"], self.path
                if m == "ok":
                    if p.startswith("/api/submissions?"):
                        d = {"data": [{"id": 7, "submitters": []}]}
                    elif p.startswith("/api/submissions/7"):
                        d = {"id": 7, "status": "completed", "submitters": [], "documents": [{"name": "doc", "url": "http://127.0.0.1:%d/dl" % port}]}
                    elif p == "/dl":
                        self.send_response(500, "ZZBODYMARKZZ" + "x" * 166 + tok.replace("\n", " "))
                        self.send_header("Content-Length", "0"); self.end_headers(); return
                    elif "/templates" in p:
                        d = {"data": [{"id": 1, "name": "NDA", "fields": [], "submitters": [{"name": "First Party"}]}], "id": 1, "name": "NDA", "submitters": [{"name": "First Party"}]}
                    elif p == "/api/submissions":
                        d = [{"submission_id": 7, "slug": "abc"}]
                    else:
                        d = {"message": []}
                    b = json.dumps(d).encode(); self.send_response(200)
                elif m == "echo500":
                    b = ("ZZBODYMARKZZ ERROR, request headers were:\n" + str(self.headers) + "\nbody:" + body.decode("utf-8", "replace")).encode(); self.send_response(500)
                elif m == "transformed":
                    b = ("ZZBODYMARKZZ " + verwandelt(tok)).encode(); self.send_response(400, "ZZBODYMARKZZ " + verwandelt(tok).replace("\n", " ")[:200])
                elif m == "json_error":
                    b = json.dumps({"exc_type": "AuthenticationError", "error": "ZZBODYMARKZZ " + tok, "message": "ZZBODYMARKZZ"}).encode(); self.send_response(401)
                elif m == "boundary":
                    pad = 395 if self.headers.get("X-Auth-Token") else 595
                    b = ("ZZBODYMARKZZ" + "y" * pad + tok + "z" * 50).encode(); self.send_response(400)
                else:  # redirect
                    self.send_response(302); self.send_header("Location", "http://127.0.0.1:9/steal?t=" + quote(tok)); b = b"moved"
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            do_GET = do_POST = do_PUT = antwort
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        zugang.einstellungen_quelle = lambda: {"docuseal_url": "http://127.0.0.1:%d" % port, "erpnext_url": "http://127.0.0.1:%d" % port,
                                               "imap_host": "127.0.0.1", "imap_user": "me", "imap_port": 9}

        def call(meth, pfad, body=None):
            r = getattr(c, meth)("/p" + pfad, **({"json": body} if body is not None else {}))
            antworten.append(r.text)
            return r
        gmail = {"m": "echo"}

        def gmail_mock(provider, methode, url, headers, timeout):
            if gmail["m"] == "echo":
                return 400, ("ZZBODYMARKZZ bad request; client " + geheim["TROVE_GMAIL_CLIENT_ID"] + " " + url).encode()
            if gmail["m"] == "transformed":
                return 403, json.dumps({"error": {"status": "PERMISSION_DENIED", "message": "ZZBODYMARKZZ " + verwandelt(geheim["TROVE_GMAIL_CLIENT_ID"])}}).encode()
            return 400, ("ZZBODYMARKZZ" + "q" * 283 + geheim["TROVE_GMAIL_CLIENT_ID"]).encode()
        zugang.anfrage_quelle = gmail_mock
        imap_modus = {"m": "err"}

        class FakeIMAP:
            def __init__(self, host, port, timeout=None):
                pass

            def login(self, u, p):
                if imap_modus["m"] == "abort":
                    raise imaplib.IMAP4.abort("server said: bad creds for " + p)
                if imap_modus["m"] == "oserr":
                    raise OSError("socket died while sending LOGIN me " + p)
                raise imaplib.IMAP4.error("LOGIN failed " + p)
        alt_imap = imaplib.IMAP4_SSL
        imaplib.IMAP4_SSL = FakeIMAP
        try:
            ein = os.path.join(tmp, "in"); os.makedirs(ein)
            with open(os.path.join(ein, "a.txt"), "w", encoding="utf-8") as f:
                f.write("Rechnung 4711 Summe 10,00 EUR")
            q = call("post", "/quellen", {"art": "ordner", "pfad": ein}).json()
            call("get", "/integrationen")
            for m in ("ok", "echo500", "boundary", "redirect", "transformed", "json_error"):
                modus["m"] = m
                call("get", "/integrationen/docuseal/vorlagen")
                call("post", "/integrationen/docuseal/senden", {"vorlage": 1, "an": "x@example.invalid", "quelle": q["id"], "pfad": "a.txt", "confirm": True})
                call("post", "/integrationen/docuseal/abholen", {})
                call("post", "/integrationen/erpnext/doctypes", {"confirm": True})
                call("post", "/integrationen/erpnext/spiegeln", {})
                call("get", "/integrationen/erpnext/historie?sha256=" + "b" * 64)
            for m in ("echo", "boundary", "transformed"):
                gmail["m"] = m
                call("post", "/integrationen/mail/abholen", {"postfach": "gmail"})
            for m in ("err", "abort", "oserr"):
                imap_modus["m"] = m
                call("post", "/integrationen/mail/abholen", {"postfach": "imap"})
            r = call("post", "/wissen", {"quelle": q["id"], "pfad": "", "sammlung": "oe1", "sichtbarkeit": "oeffentlich", "zugehoerigkeit": "Example Ltd", "ohne_embed": True})
            assert r.status_code == 200, r.text
            e = call("post", "/integrationen/reraise/probe", {"quelle": q["id"], "pfad": "", "sammlung": "oe1"}).json()["eintraege"][0]
            best = [e["sha256"] + ":" + e["karte_hash"]]
            k = geheim["TROVE_RERAISE_KEY"]
            rh.OPENER = rh._Netz({"cards": []}, rh._http(400, {"error": "bad", "message": "ZZBODYMARKZZ invalid key " + verwandelt(k) + " for origin"}), mandant=mod.MANDANT)
            call("post", "/integrationen/reraise/senden", {"quelle": q["id"], "pfad": "", "sammlung": "oe1", "bestaetigt": best})
            rh.OPENER = rh._Netz({"cards": []}, rh._http(500, {"message": "ZZBODYMARKZZ" + "y" * 268 + k}), mandant=mod.MANDANT)
            call("post", "/integrationen/reraise/senden", {"quelle": q["id"], "pfad": "", "sammlung": "oe1", "bestaetigt": best})
        finally:
            imaplib.IMAP4_SSL = alt_imap
            srv.shutdown()
            root.removeHandler(fang)
        daten = []
        for d, _, fs in os.walk(os.path.join(tmp, "data")):
            for f in fs:
                with open(os.path.join(d, f), encoding="utf-8", errors="replace") as fh:
                    daten.append(fh.read())
        quellen = {"responses": "\n".join(antworten), "logging": "\n".join(log), "datafiles": "\n".join(daten),
                   "erpnext_received": "\n".join(x for x in empfangen if "insert_many" in x or "Trove Beleg" in x or "doctype" in x)}
        funde = []
        for name, text in quellen.items():
            if "AuthenticationError" in text:  # K8: no error code from an upstream body either
                funde.append("%s upstream error code passed on" % name)
            if "ZZBODYMARKZZ" in text:
                i = text.index("ZZBODYMARKZZ")
                funde.append("%s upstream body passed on: %r" % (name, text[max(0, i - 80):i + 40]))
            for gname, wert in geheim.items():
                for v in verwandelt_alle(wert):
                    if v in text and name != "erpnext_received":
                        funde.append("%s %s transformed form %r" % (name, gname, v[:20]))
            for gname, wert in geheim.items():
                stuecke = set()
                for zeile in wert.split("\n"):
                    for form_ in (zeile, json.dumps(zeile)[1:-1], repr(zeile)[1:-1], quote(zeile, safe=""), quote_plus(zeile)):
                        stuecke |= {form_[i:i + 6] for i in range(len(form_) - 5)}
                stuecke = {s for s in stuecke if not re.fullmatch(r"[a-z0-9]+|\W+", s)}  # pieces that could occur by chance
                treffer = sorted(s for s in stuecke if s in text)
                if treffer:
                    funde.append("%s %s %s" % (name, gname, treffer[:5]))
        ok = len(antworten) > 30 and not funde
        print(("PASS " if ok else "FAIL ") + "%s: %d responses, %d log lines, %d data files scanned" % (form, len(antworten), len(log), len(daten)))
        for f in funde[:20]:
            print("     LEAK " + f)
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    erg = [lauf(f) for f in FORMEN] + [kalter_spiegel(f) for f in FORMEN]
    print("ALL PASSED" if all(erg) else "FAILED")
    sys.exit(0 if all(erg) else 1)


if __name__ == "__main__":
    main()
