"""Trove F11 — Spiegel des Belegprotokolls in ERPNext (A-09) und Historie je Dokument.

Datei bleibt die Wahrheit (F8); ERPNext bekommt einen Spiegel fuer Suche und Verknuepfung im Betrieb:
  Doctype "Trove Belegereignis": eine Zeile je Protokollzeile (nr, zeit, ereignis, quelle, ziel, sha256, ...) — nur anfuegend.
  Doctype "Trove Beleg": ein Satz je Inhalt (sha256) mit letztem Ort, erster Sicht, Anzahl Ereignisse und den Feldern aus dem
  Zwilling (Datum, Brutto, Waehrung, Rechnungsnummer, Aussteller-Kennung) — die Geschaeftshistorie aus dem Inhalt.
Stand je Mandant: <TROVE_DATEN>\\<mandant>\\spiegel_erpnext.json (letzte gespiegelte nr) und spiegel_belege.json (Marke je sha256); je ein Lauf
(belegprotokoll.Sperre art "lauf" auf spiegel_erpnext.lock / spiegel_belege.lock; belegt -> LaufLaeuft).
Zugang (je Aufruf frisch, Reihenfolge Umgebung > trove.json "umgebung" > Standard nur unter Windows):
  TROVE_ERPNEXT       Basis-URL            (Pflichtangabe, keine Vorgabe)
  TROVE_ERPNEXT_HOST  Host-Kopf, optional  (keine Vorgabe)
  TROVE_ERPNEXT_KEYS  Ordner mit api_key.txt / api_secret.txt (Pflichtangabe)
Schluessel nie im Code, nie im Repo. Auf Mac/Linux ohne Konfiguration -> NichtEingerichtet (kein stiller Windows-Pfad).
Einrichten (einmalig, mit Administrator-Zugang): Doctypes wie auf dem Server, Rolle "Trove Spiegel" nur fuer diese zwei Doctypes,
Agentenbenutzer trove-spiegel@pe.local nur mit dieser Rolle, API-Schluessel in den Schluesselordner (0600), trove.json "umgebung".
"""
import argparse, http.cookiejar, json, os, sys, time, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "plugin", "dashboard"))  # Trove core modules
import belegprotokoll as bp
import konfig

DT_EREIGNIS, DT_BELEG = "Trove Belegereignis", "Trove Beleg"
ROLLE, AGENT = "Trove Spiegel", "trove-spiegel@pe.local"
WINDOWS_STANDARD = {}


class NichtEingerichtet(RuntimeError):
    pass


def einstellung(var):
    """Umgebung > trove.json "umgebung" (frisch gelesen, wirkt ohne Neuladen) > Windows-Standard (nur win32)."""
    w = os.environ.get(var)
    if w:
        return w
    try:
        w = (konfig.lesen().get("umgebung") or {}).get(var)
    except (OSError, ValueError):
        w = None
    if w:
        return str(w)
    return WINDOWS_STANDARD.get(var) if sys.platform == "win32" else None


def ziel():
    url, keys = einstellung("TROVE_ERPNEXT"), einstellung("TROVE_ERPNEXT_KEYS")
    if not url or not keys:
        raise NichtEingerichtet("ERPNext-Spiegel nicht eingerichtet: TROVE_ERPNEXT und TROVE_ERPNEXT_KEYS fehlen (trove.json umgebung) - "
                                "einmalig: spiegel_erpnext.py einrichten --url ... --schluessel ...")
    return url.rstrip("/"), einstellung("TROVE_ERPNEXT_HOST"), keys


def _kopf():
    url, host, keys = ziel()
    k = open(os.path.join(keys, "api_key.txt")).read().strip()
    s = open(os.path.join(keys, "api_secret.txt")).read().strip()
    h = {"Authorization": "token %s:%s" % (k, s), "Content-Type": "application/json", "Accept": "application/json"}
    if host:
        h["Host"] = host
    return url, h


def api(path, body=None, method=None, timeout=120, zugang=None):
    """zugang: (url, kopf, oeffner) fuer den Einrichter (Administrator-Sitzung); sonst Agenten-Schluessel aus der Konfiguration."""
    url, kopf, oeffner = zugang if zugang else (*_kopf(), urllib.request.urlopen)
    req = urllib.request.Request(url + path, data=json.dumps(body).encode() if body is not None else None, method=method or ("POST" if body is not None else "GET"), headers=kopf)
    try:
        with oeffner(req, timeout=timeout) as h:
            return h.status, json.loads(h.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:600]


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


def doctypes_anlegen(zugang=None):
    """Legt fehlende Doctypes an, ruestet "zeile" nach und traegt die Agentenrolle nach (idempotent). Braucht Administrator-Rechte."""
    q = lambda *a, **k: api(*a, zugang=zugang, **k)
    if not q("/api/resource/Role/" + urllib.parse.quote(ROLLE))[0] == 200:
        st, r = q("/api/resource/Role", {"role_name": ROLLE, "desk_access": 1})
        if st != 200:
            raise RuntimeError("Rolle %s: %s %s" % (ROLLE, st, str(r)[:200]))
    st, r = q("/api/resource/DocType?filters=" + urllib.parse.quote(json.dumps([["name", "like", "Trove%"]])) + "&fields=" + urllib.parse.quote(json.dumps(["name"])))
    if st != 200:
        raise RuntimeError("DocType-Liste: %s %s" % (st, str(r)[:200]))
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


def _admin_sitzung(url, benutzer, passwort, host=None):
    oeffner = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())).open
    kopf = {"Content-Type": "application/json", "Accept": "application/json", **({"Host": host} if host else {})}
    st, r = api("/api/method/login", {"usr": benutzer, "pwd": passwort}, zugang=(url, kopf, oeffner))
    if st != 200:
        raise RuntimeError("Administrator-Anmeldung abgelehnt (%s)" % st)
    return url, kopf, oeffner


def zugang_aus_datei(pfad):
    """PE-Zugangsdatei (Zeilen "Schluessel: Wert"); liefert nur Administrator-Benutzer/-Passwort, gibt nichts aus."""
    z = {}
    with open(pfad, encoding="utf-8") as f:
        for l in f:
            if ":" in l:
                k, v = l.split(":", 1)
                z[k.strip()] = v.strip()
    return z.get("Administrator-Benutzer") or "Administrator", z["Administrator-Passwort"]


def _agent_prueft(url, host, keys):
    try:
        k = open(os.path.join(keys, "api_key.txt")).read().strip(); s = open(os.path.join(keys, "api_secret.txt")).read().strip()
    except OSError:
        return False
    kopf = {"Authorization": "token %s:%s" % (k, s), "Accept": "application/json", **({"Host": host} if host else {})}
    st, r = api("/api/method/frappe.auth.get_logged_user", zugang=(url, kopf, urllib.request.urlopen))
    return st == 200 and r.get("message") == AGENT


def einrichten(url, schluessel, benutzer, passwort, host=None, konfig_schreiben=True):
    """Einmalig mit Administrator-Zugang: Rolle + Doctypes, Agentenbenutzer nur mit ROLLE, Schluessel (0600), trove.json umgebung."""
    url = url.rstrip("/")
    za = _admin_sitzung(url, benutzer, passwort, host)
    erg = {"doctypes": doctypes_anlegen(za)}
    q = lambda *a, **k: api(*a, zugang=za, **k)
    st, r = q("/api/resource/User/" + urllib.parse.quote(AGENT))
    if st != 200:
        st, r = q("/api/resource/User", {"email": AGENT, "first_name": "Trove Spiegel", "user_type": "System User", "send_welcome_email": 0, "enabled": 1, "roles": [{"role": ROLLE}]})
        if st != 200:
            raise RuntimeError("Agentenbenutzer: %s %s" % (st, str(r)[:200]))
        erg["agent"] = "angelegt"
    else:
        rollen = sorted(x["role"] for x in r["data"].get("roles", []))
        if rollen != [ROLLE]:  # nur diese eine Rolle (minimal)
            st, r = q("/api/resource/User/" + urllib.parse.quote(AGENT), {"roles": [{"role": ROLLE}], "user_type": "System User", "enabled": 1}, "PUT")
        erg["agent"] = "vorhanden"
    os.makedirs(schluessel, exist_ok=True)
    os.chmod(schluessel, 0o700)
    if _agent_prueft(url, host, schluessel):
        erg["schluessel"] = "vorhanden und gueltig"
    else:
        st, r = q("/api/method/frappe.core.doctype.user.user.generate_keys", {"user": AGENT})
        if st != 200:
            raise RuntimeError("Schluessel erzeugen: %s" % st)
        geheim = r["message"]["api_secret"]
        schl = r["message"].get("api_key") or q("/api/resource/User/" + urllib.parse.quote(AGENT))[1]["data"]["api_key"]
        for name, wert in (("api_key.txt", schl), ("api_secret.txt", geheim)):
            p = os.path.join(schluessel, name)
            fd = os.open(p + ".neu", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(wert)
            os.replace(p + ".neu", p)
        erg["schluessel"] = "neu erzeugt" if _agent_prueft(url, host, schluessel) else "erzeugt, aber Anmeldung scheitert"
    if konfig_schreiben:
        k = konfig.lesen()
        u = dict(k.get("umgebung") or {})
        u.update({"TROVE_ERPNEXT": url, "TROVE_ERPNEXT_KEYS": schluessel})
        if host:
            u["TROVE_ERPNEXT_HOST"] = host
        k["umgebung"] = u
        p = konfig.konfig_pfad()
        with open(p + ".neu", "w", encoding="utf-8") as f:
            json.dump(k, f, ensure_ascii=False, indent=1)
        os.replace(p + ".neu", p)
        erg["konfig"] = p
    return erg


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
                raise RuntimeError("insert_many fehlgeschlagen bei Zeile %d: %s %s" % (teil[0]["_zeile"], st, str(r)[:300]))
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
                raise RuntimeError("get_list Belege: %s %s" % (st, str(r)[:300]))
            da = {x["name"] for x in r.get("message", [])}
            neu = [d for sha, m, d in teil if sha not in da]
            if neu:
                st, r = api("/api/method/frappe.client.insert_many", {"docs": neu}, timeout=600)
                if st != 200:
                    raise RuntimeError("insert_many Belege: %s %s" % (st, str(r)[:300]))
            for sha, m, d in teil:
                if sha in da:
                    st, r = api("/api/resource/%s/%s" % (urllib.parse.quote(DT_BELEG), sha), {k: v for k, v in d.items() if k not in ("doctype", "sha256")}, method="PUT")
                    if st != 200:
                        raise RuntimeError("PUT Beleg %s: %s %s" % (sha[:12], st, str(r)[:200]))
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


def main():
    if sys.argv[1:] == ["--selbsttest"]:
        _selbsttest()
        print("selbsttest ok")
        return
    ap = argparse.ArgumentParser(description="Trove F11 ERPNext-Spiegel")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctypes")
    s = sub.add_parser("einrichten", help="einmalig mit Administrator-Zugang (Datei oder TROVE_ERPNEXT_ADMIN_PASSWORT)")
    s.add_argument("--url", required=True); s.add_argument("--schluessel", required=True); s.add_argument("--host")
    s.add_argument("--zugang", help="PE-Zugangsdatei mit Administrator-Benutzer/-Passwort"); s.add_argument("--ohne-konfig", action="store_true")
    s = sub.add_parser("spiegeln"); s.add_argument("--mandant", default=bp.konfig.mandant()); s.add_argument("--max", type=int)
    s = sub.add_parser("belege"); s.add_argument("--mandant", default=bp.konfig.mandant()); s.add_argument("--max", type=int)
    s = sub.add_parser("historie"); s.add_argument("sha256")
    s = sub.add_parser("zaehlen")
    a = ap.parse_args()
    if a.cmd == "einrichten":
        if a.zugang:
            ben, pw = zugang_aus_datei(a.zugang)
        else:
            ben, pw = os.environ.get("TROVE_ERPNEXT_ADMIN_BENUTZER", "Administrator"), os.environ["TROVE_ERPNEXT_ADMIN_PASSWORT"]
        print(json.dumps(einrichten(a.url, os.path.expanduser(a.schluessel), ben, pw, a.host, not a.ohne_konfig), ensure_ascii=False))
    elif a.cmd == "doctypes":
        print(json.dumps(doctypes_anlegen(), ensure_ascii=False))
    elif a.cmd == "spiegeln":
        r = spiegeln(a.mandant, a.max)
        bp.schreiben(bp.pfad_fuer(a.mandant), ereignis="notiz", werkzeug="spiegel_erpnext.py", mandant=a.mandant, meldung="ERPNext-Spiegel: %d Zeilen gespiegelt bis Zeile %d (nr %d)" % (r["gespiegelt"], r["letzte_zeile"], r["letzte_nr"]))
        print(json.dumps(r))
    elif a.cmd == "belege":
        print(json.dumps(belege_aktualisieren(a.mandant, a.max)))
    elif a.cmd == "historie":
        print(json.dumps(historie(a.sha256), ensure_ascii=False, indent=1))
    elif a.cmd == "zaehlen":
        for dt in (DT_EREIGNIS, DT_BELEG):
            st, r = api("/api/method/frappe.client.get_count?doctype=" + urllib.parse.quote(dt))
            print(dt, st, r.get("message") if st == 200 else r)


if __name__ == "__main__":
    main()
