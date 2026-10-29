"""Trove F11 — mirror of the receipt log into the user's OWN ERPNext, plus history per document (optional, inactive by default).

The file stays the truth (F8); ERPNext, the user's accounting system, gets a mirror for search and linking:
  Doctype "Trove Belegereignis": one row per log line (nr, time, event, source, target, sha256, ...), append-only.
  Doctype "Trove Beleg": one record per content (sha256) with last location, first sighting, number of events and the fields of the
  twin (date, gross, currency, invoice number, issuer id).
State per client: <TROVE_DATEN>/<client>/spiegel_erpnext.json (last mirrored line) and spiegel_belege.json (mark per sha256); one run at a
time (belegprotokoll.Sperre "lauf" on spiegel_erpnext.lock / spiegel_belege.lock; busy -> LaufLaeuft).
Configuration only through Hermes: setting erpnext_url (no default), optional erpnext_host_header, secrets TROVE_ERPNEXT_API_KEY and
TROVE_ERPNEXT_API_SECRET (an API key pair the user creates in ERPNext for a user with the role "Trove Spiegel"). Without them every call
raises zugang.NichtEingerichtet and nothing goes out. Trove never asks for an administrator password; doctypes_anlegen() uses the
configured key and needs a key whose user may create doctypes (or the user creates the two doctypes by hand).
"""
import json, os, sys, urllib.error, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import belegprotokoll as bp
import zugang
from zugang import NichtEingerichtet

DT_EREIGNIS, DT_BELEG = "Trove Belegereignis", "Trove Beleg"
ROLLE = "Trove Spiegel"


def eingerichtet():
    """(active, missing settings) without any network call."""
    fehlt = []
    try:
        if not zugang.url_einstellung("erpnext_url"):
            fehlt.append("erpnext_url")
    except NichtEingerichtet:
        fehlt.append("erpnext_url")
    for n in (zugang.ERPNEXT_KEY, zugang.ERPNEXT_SECRET):
        if not zugang.ist_gesetzt(n):
            fehlt.append(n)
    return not fehlt, fehlt


def _kopf():
    url = zugang.url_einstellung("erpnext_url")
    k, s = zugang.geheimnis(zugang.ERPNEXT_KEY), zugang.geheimnis(zugang.ERPNEXT_SECRET)
    if not (url and k and s):
        raise NichtEingerichtet("ERPNext mirror is not configured in Hermes (erpnext_url, %s, %s)" % (zugang.ERPNEXT_KEY, zugang.ERPNEXT_SECRET))
    h = {"Authorization": "token %s:%s" % (k, s), "Content-Type": "application/json", "Accept": "application/json"}
    host = str(zugang.einstellung("erpnext_host_header") or "").strip()
    if host:
        h["Host"] = host
    return url, h


class _KeinUmleiten(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None  # a redirect would carry the key elsewhere


OPENER = urllib.request.build_opener(_KeinUmleiten)


def api(path, body=None, method=None, timeout=120):
    url, kopf = _kopf()
    req = urllib.request.Request(url + path, data=json.dumps(body).encode() if body is not None else None, method=method or ("POST" if body is not None else "GET"), headers=kopf)
    try:
        with OPENER.open(req, timeout=timeout) as h:
            return h.status, json.loads(h.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, zugang.schwaerzen(e.read().decode("utf-8", "replace")[:600])


def feld(fieldname, label, fieldtype="Data", **kw):
    d = {"fieldname": fieldname, "label": label, "fieldtype": fieldtype}
    d.update(kw)
    return d


RECHTE = [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 0, "export": 1, "report": 1},
          {"role": "Accounts User", "read": 1, "write": 1, "create": 1, "delete": 1, "export": 1, "report": 1}]
RECHTE_AGENT = {"role": ROLLE, "read": 1, "write": 1, "create": 1, "delete": 0, "export": 0, "report": 1}  # anfuegen + Beleg-Satz nachfuehren, nie loeschen


def definitionen():
    """Feld fuer Feld wie auf dem Server-ERPNext (per API ausgelesen 2026-10-19), plus Agentenrolle."""
    return {
        DT_EREIGNIS: {
            "name": DT_EREIGNIS, "module": "Custom", "custom": 1, "autoname": "format:TRV-{mandant}-{zeile}", "track_changes": 0, "sort_field": "zeile", "sort_order": "DESC",
            "title_field": "ereignis", "search_fields": "sha256,quelle,ziel", "in_create": 0, "allow_rename": 1,
            "fields": [feld("mandant", "Mandant", in_list_view=1, reqd=1, in_standard_filter=1), feld("zeile", "Zeile", "Int", in_list_view=1), feld("nr", "Nr", "Int", in_list_view=1, reqd=1),
                       feld("zeit", "Zeit", in_list_view=1), feld("ereignis", "Ereignis", in_list_view=1, in_standard_filter=1),
                       feld("quelle", "Quelle", "Small Text"), feld("ziel", "Ziel", "Small Text"), feld("sha256", "sha256", in_standard_filter=1),
                       feld("bytes", "Bytes", "Int"), feld("werkzeug", "Werkzeug"), feld("lauf", "Lauf"), feld("meldung", "Meldung", "Small Text"),
                       feld("kette", "Kette"), feld("extra", "Weitere Felder", "Code", options="JSON")],
            "permissions": RECHTE + [RECHTE_AGENT]},
        DT_BELEG: {
            "name": DT_BELEG, "module": "Custom", "custom": 1, "autoname": "field:sha256", "title_field": "dateiname", "search_fields": "dateiname,letzter_ort,rechnungsnummer",
            "track_changes": 0, "sort_field": "creation", "sort_order": "DESC", "in_create": 0, "allow_rename": 1,
            "fields": [feld("sha256", "sha256", reqd=1, unique=1), feld("mandant", "Mandant", in_list_view=1, in_standard_filter=1), feld("dateiname", "Dateiname", in_list_view=1),
                       feld("letzter_ort", "Letzter Ort", "Small Text"), feld("erste_sicht", "Erste Sicht"), feld("letzte_bewegung", "Letzte Bewegung"), feld("ereignisse", "Ereignisse", "Int"),
                       feld("bytes", "Bytes", "Int"), feld("orte", "Bekannte Orte", "Small Text"),
                       feld("sektion", "Geschaeftshistorie aus Inhalt", "Section Break"), feld("belegdatum", "Belegdatum", "Date", in_list_view=1), feld("brutto", "Brutto", "Currency", in_list_view=1),
                       feld("netto", "Netto", "Currency"), feld("steuer", "Steuer", "Currency"), feld("waehrung", "Waehrung"), feld("rechnungsnummer", "Rechnungsnummer"),
                       feld("aussteller_kennung", "Aussteller-Kennung (CUIT/USt-IdNr)"), feld("lesemethode", "Lesemethode"), feld("verprobung", "Verprobung"), feld("zwilling", "Zwilling (Pfad)", "Small Text")],
            "permissions": RECHTE + [RECHTE_AGENT]}}


def doctypes_anlegen():
    """Creates missing doctypes, adds "zeile" and the mirror role (idempotent). Needs a key whose user may create doctypes."""
    q = api
    if not q("/api/resource/Role/" + urllib.parse.quote(ROLLE))[0] == 200:
        st, r = q("/api/resource/Role", {"role_name": ROLLE, "desk_access": 1})
        if st != 200:
            raise RuntimeError("ERPNext role %s: %s %s" % (ROLLE, st, str(r)[:200]))
    st, r = q("/api/resource/DocType?filters=" + urllib.parse.quote(json.dumps([["name", "like", "Trove%"]])) + "&fields=" + urllib.parse.quote(json.dumps(["name"])))
    if st != 200:
        raise RuntimeError("ERPNext doctype list: %s %s" % (st, str(r)[:200]))
    vorhanden = {x["name"] for x in r.get("data", [])}
    ergebnis = {}
    for name, d in definitionen().items():
        if name not in vorhanden:
            st, r = q("/api/resource/DocType", d)
            ergebnis[name] = st if st == 200 else (st, str(r)[:300])
    # Nachruesten: Feld "zeile" + Namensregel nach Zeilenposition (nr kann nach vermerktem Kettenbruch doppelt vorkommen)
    st, dt = q("/api/resource/DocType/" + urllib.parse.quote(DT_EREIGNIS))
    if st == 200:
        d = dt["data"]
        if not any(f["fieldname"] == "zeile" for f in d["fields"]):
            d["fields"].insert(1, feld("zeile", "Zeile", "Int", in_list_view=1))
            st, r = q("/api/resource/DocType/" + urllib.parse.quote(DT_EREIGNIS), {"fields": d["fields"], "autoname": "format:TRV-{mandant}-{zeile}", "sort_field": "zeile"}, "PUT")
            ergebnis["nachgeruestet_zeile"] = st if st == 200 else (st, str(r)[:200])
    for name in definitionen():
        st, dt = q("/api/resource/DocType/" + urllib.parse.quote(name))
        if st == 200 and not any(p.get("role") == ROLLE for p in dt["data"]["permissions"]):
            st, r = q("/api/resource/DocType/" + urllib.parse.quote(name), {"permissions": dt["data"]["permissions"] + [RECHTE_AGENT]}, "PUT")
            ergebnis["rolle_" + name] = st if st == 200 else (st, str(r)[:200])
    return {"vorhanden": sorted(vorhanden), "angelegt": ergebnis}


def _stand_pfad(mandant, name="spiegel_erpnext.json"):
    return os.path.join(bp.WURZEL, mandant, name)


def stand(mandant, name="spiegel_erpnext.json", leer=None):
    """Stand immer frisch von Platte lesen (Server und Hintergrundlauf koennen nebeneinander schreiben)."""
    p = _stand_pfad(mandant, name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else (leer if leer is not None else {"letzte_nr": 0})


def stand_speichern(mandant, s, name="spiegel_erpnext.json"):
    p = _stand_pfad(mandant, name)
    with open(p + ".neu", "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)
    os.replace(p + ".neu", p)


def _sperre(mandant, name):
    """Ein Spiegel-Lauf je Mandant und Art (belegprotokoll.Sperre, Handle offen bis zum Ende)."""
    return bp.Sperre(_stand_pfad(mandant, name + ".lock"), art="lauf")


KERN = ("nr", "zeit", "ereignis", "quelle", "ziel", "sha256", "bytes", "werkzeug", "mandant", "lauf", "meldung", "kette")


def spiegeln(mandant, max_zeilen=None, batch=200):
    with _sperre(mandant, "spiegel_erpnext"):
        s = stand(mandant)
        if "letzte_zeile" not in s:  # Altstand (nach nr): bis dahin war nr == Zeile (vor dem ersten vermerkten Bruch)
            s["letzte_zeile"] = int(s.get("letzte_nr", 0))
        st, r = api("/api/method/frappe.client.get_list", {"doctype": DT_EREIGNIS, "filters": [["mandant", "=", mandant], ["zeile", ">", 0]], "fields": ["zeile"], "order_by": "zeile desc", "limit_page_length": 1})
        if st == 200 and r.get("message"):  # ERPNext kennt den wahren Stand (Lauf starb zwischen insert und stand_speichern)
            s["letzte_zeile"] = max(s["letzte_zeile"], int(r["message"][0]["zeile"]))
        pfad = bp.pfad_fuer(mandant)
        neu = []
        for zeile, e in enumerate(bp.eintraege(pfad), 1):
            if zeile > s["letzte_zeile"]:
                e = dict(e); e["_zeile"] = zeile
                neu.append(e)
                if max_zeilen and len(neu) >= max_zeilen:
                    break
        gesendet = 0
        for i in range(0, len(neu), batch):
            teil = neu[i:i + batch]
            docs = []
            for e in teil:
                d = {"doctype": DT_EREIGNIS, "zeile": e["_zeile"]}
                for k in KERN:
                    d[k] = e.get(k) if k != "mandant" else (e.get("mandant") or mandant)
                if not isinstance(d.get("bytes"), int) or d["bytes"] > 2147483647:
                    d["bytes"] = 0  # Int-Spalte (NOT NULL, 32 Bit in MariaDB): 0 = unbekannt/zu gross; echter Wert steht in extra
                d["extra"] = json.dumps({k: v for k, v in e.items() if (k not in KERN and k != "_zeile") or (k == "bytes" and v != d["bytes"])}, ensure_ascii=False) or None
                docs.append(d)
            st, r = api("/api/method/frappe.client.insert_many", {"docs": docs}, timeout=600)
            if st != 200:
                raise RuntimeError("ERPNext insert_many failed at line %d: %s %s" % (teil[0]["_zeile"], st, str(r)[:300]))
            gesendet += len(teil)
            s["letzte_zeile"] = teil[-1]["_zeile"]; s["letzte_nr"] = teil[-1]["nr"]
            stand_speichern(mandant, s)
        return {"gespiegelt": gesendet, "letzte_zeile": s["letzte_zeile"], "letzte_nr": s["letzte_nr"]}


def belege_aktualisieren(mandant, max_belege=None):
    """Ein Satz je Inhalt (sha256): aus den Ereignissen letzter Ort, Orte, Zeiten; aus dem Zwilling die Geschaeftsfelder."""
    pfad = bp.pfad_fuer(mandant)
    sicht = {}
    for e in bp.eintraege(pfad):
        sha = e.get("sha256")
        if not sha or e.get("ereignis") in ("notiz",):
            continue
        b = sicht.setdefault(sha, {"erste_sicht": e["zeit"], "orte": set(), "ereignisse": 0, "bytes": e.get("bytes")})
        b["ereignisse"] += 1; b["letzte_bewegung"] = e["zeit"]
        # bei gelesen/verprobt ist das Ziel der Zwilling, nicht der Beleg -> nur die Quelle zaehlt als Ort
        orte = [e.get("quelle")] if e.get("ereignis") in ("gelesen", "verprobt") else [e.get("ziel"), e.get("quelle")]
        for o in orte:
            if o:
                b["orte"].add(o)
        if orte and orte[0]:
            b["letzter_ort"] = orte[0]
        if e.get("bytes"):
            b["bytes"] = e["bytes"]
    zw = os.path.join(bp.WURZEL, mandant, "zwillinge")
    pruef = {}
    pl = os.path.join(bp.WURZEL, mandant, "pruefliste.jsonl")
    if os.path.exists(pl):
        for z in bp.eintraege(pl):
            pruef[z.get("sha256")] = ("sicher" if z.get("sicher") else "bestanden" if z.get("bestanden") else "Prueflist: " + "; ".join(z.get("gruende", [])))[:140]
    with _sperre(mandant, "spiegel_belege"):
        s = stand(mandant, "spiegel_belege.json", {"belege": {}})
        gesendet = 0
        shas = list(sicht)
        if max_belege:
            shas = shas[:max_belege]
        docs = []
        for sha in shas:
            b = sicht[sha]
            zp = os.path.join(zw, sha + ".json")
            marke = "%s|%d" % (b["letzte_bewegung"], b["ereignisse"]) + ("|" + pruef[sha] if sha in pruef else "")
            if s["belege"].get(sha) == marke:
                continue
            d = {"doctype": DT_BELEG, "sha256": sha, "mandant": mandant, "dateiname": os.path.basename((b.get("letzter_ort") or "").replace("/", "\\"))[:140], "letzter_ort": b.get("letzter_ort"),
                 "erste_sicht": b["erste_sicht"], "letzte_bewegung": b["letzte_bewegung"], "ereignisse": b["ereignisse"], "bytes": (b.get("bytes") if isinstance(b.get("bytes"), int) and b["bytes"] <= 2147483647 else 0), "orte": "\n".join(sorted(b["orte"]))[:2000]}
            if os.path.exists(zp):
                z = json.load(open(zp, encoding="utf-8")); f = z.get("felder", {})
                d.update({"belegdatum": f.get("datum"), "brutto": f.get("brutto"), "netto": f.get("netto"), "steuer": f.get("steuer"), "waehrung": f.get("waehrung"),
                          "rechnungsnummer": f.get("rechnungsnummer"), "aussteller_kennung": f.get("cuit") or f.get("ust_id"), "lesemethode": z.get("methode"), "zwilling": zp, "verprobung": pruef.get(sha)})
            docs.append((sha, marke, d))
        for i in range(0, len(docs), 100):
            teil = docs[i:i + 100]
            st, r = api("/api/method/frappe.client.get_list", {"doctype": DT_BELEG, "filters": [["sha256", "in", [t[0] for t in teil]]], "fields": ["name"], "limit_page_length": 1000})
            if st != 200:
                raise RuntimeError("ERPNext get_list documents: %s %s" % (st, str(r)[:300]))
            da = {x["name"] for x in r.get("message", [])}
            neu = [d for sha, m, d in teil if sha not in da]
            if neu:
                st, r = api("/api/method/frappe.client.insert_many", {"docs": neu}, timeout=600)
                if st != 200:
                    raise RuntimeError("ERPNext insert_many documents: %s %s" % (st, str(r)[:300]))
            for sha, m, d in teil:
                if sha in da:
                    st, r = api("/api/resource/%s/%s" % (urllib.parse.quote(DT_BELEG), sha), {k: v for k, v in d.items() if k not in ("doctype", "sha256")}, method="PUT")
                    if st != 200:
                        raise RuntimeError("ERPNext update document %s: %s %s" % (sha[:12], st, str(r)[:200]))
                s["belege"][sha] = m
            gesendet += len(teil)
            stand_speichern(mandant, s, "spiegel_belege.json")
        return {"belege_aktualisiert": gesendet, "belege_gesamt": len(sicht)}


def historie(sha):
    st, r = api("/api/resource/%s?filters=%s&fields=%s&order_by=creation%%20asc&limit_page_length=500" % (urllib.parse.quote(DT_EREIGNIS), urllib.parse.quote(json.dumps([["sha256", "=", sha]])), urllib.parse.quote(json.dumps(["nr", "zeit", "ereignis", "quelle", "ziel", "meldung"]))))
    st2, b = api("/api/resource/%s/%s" % (urllib.parse.quote(DT_BELEG), sha))
    return {"ereignisse": r.get("data", []) if st == 200 else r, "beleg": b.get("data") if st2 == 200 else None}


def _selbsttest():
    """Laufsperre: ein zweiter Spiegel-Lauf scheitert mit LaufLaeuft, bevor ERPNext gefragt wird."""
    import tempfile
    g = globals()
    alt = g["api"], bp.WURZEL
    g["api"] = lambda *a, **k: (_ for _ in ()).throw(AssertionError("ERPNext trotz Sperre gefragt"))
    try:
        with tempfile.TemporaryDirectory() as w:
            bp.WURZEL = w
            bp.schreiben(bp.pfad_fuer("t"), ereignis="notiz", meldung="test")
            for name, f in (("spiegel_erpnext", lambda: spiegeln("t")), ("spiegel_belege", lambda: belege_aktualisieren("t"))):
                with _sperre("t", name):
                    try:
                        f()
                        raise AssertionError("zweiter Lauf %s nicht gesperrt" % name)
                    except bp.LaufLaeuft:
                        pass
    finally:
        g["api"], bp.WURZEL = alt


def _selbsttest_zugang():
    """Inactive without Hermes configuration (no network); with mocked Hermes secrets the key pair goes only to the configured URL
    and never into the result."""
    import http.server, tempfile, threading
    alt = zugang.geheimnis_quelle, zugang.einstellungen_quelle, bp.WURZEL
    anfragen = []

    class H(http.server.BaseHTTPRequestHandler):
        def antwort(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            anfragen.append((self.command, self.path, self.headers.get("Authorization"), body))
            d = {"message": []} if "get_list" in self.path else {"message": "ok"}
            b = json.dumps(d).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        do_GET = do_POST = do_PUT = antwort

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as w:
            bp.WURZEL = w
            bp.schreiben(bp.pfad_fuer("t"), ereignis="notiz", meldung="test")
            zugang.geheimnis_quelle = lambda n: None
            zugang.einstellungen_quelle = lambda: {}
            assert eingerichtet() == (False, ["erpnext_url", zugang.ERPNEXT_KEY, zugang.ERPNEXT_SECRET])
            try:
                spiegeln("t")
                raise AssertionError("mirror ran without configuration")
            except NichtEingerichtet:
                pass
            assert not anfragen
            zugang.geheimnis_quelle = lambda n: {zugang.ERPNEXT_KEY: "erp-key-1234", zugang.ERPNEXT_SECRET: "erp-secret-5678"}.get(n)
            zugang.einstellungen_quelle = lambda: {"erpnext_url": "http://127.0.0.1:%d" % srv.server_address[1]}
            assert eingerichtet() == (True, [])
            r = spiegeln("t")
            assert r["gespiegelt"] == 1 and "erp-secret-5678" not in json.dumps(r), r
            ins = [a for a in anfragen if "insert_many" in a[1]]
            assert ins and ins[0][2] == "token erp-key-1234:erp-secret-5678" and ins[0][3]["docs"][0]["ereignis"] == "notiz", anfragen
    finally:
        srv.shutdown()
        zugang.geheimnis_quelle, zugang.einstellungen_quelle, bp.WURZEL = alt


if __name__ == "__main__":
    if sys.argv[1:] == ["--selbsttest"]:
        _selbsttest()
        _selbsttest_zugang()
        print("selbsttest ok")
