"""Trove F7 — send for signature and fetch signed documents through the user's own DocuSeal (optional, inactive by default).

Direction 1 (send): create a submission from a DocuSeal template (signer, optional e-mail by DocuSeal); receipt log "versendet",
  linked to the original by its sha256.
Direction 2 (receive): poll all submissions; completed ones: signed PDFs + audit log to
  <TROVE_DATEN>/<client>/unterschrieben/vorgang_<id>/, checksum, receipt-log event "unterschrieben" (back to the original).
Configuration only through Hermes: setting docuseal_url (the user's own DocuSeal, no default) and the secret
  TROVE_DOCUSEAL_API_KEY (zugang.geheimnis). Without both, every call raises zugang.NichtEingerichtet and nothing goes out.
The key is sent only to the configured DocuSeal origin, never to download URLs on other hosts.
State per client: <TROVE_DATEN>/<client>/docuseal.json (submission -> original sha, status, fetched).
"""
import hashlib, json, os, re, sys, urllib.error, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import belegprotokoll as bp
import zugang
from zugang import NichtEingerichtet


def eingerichtet():
    """(active, missing settings) without any network call."""
    fehlt = []
    try:
        if not zugang.url_einstellung("docuseal_url"):
            fehlt.append("docuseal_url")
    except NichtEingerichtet:
        fehlt.append("docuseal_url")
    if not zugang.ist_gesetzt(zugang.DOCUSEAL_KEY):
        fehlt.append(zugang.DOCUSEAL_KEY)
    return not fehlt, fehlt


def _ziel():
    url, key = zugang.url_einstellung("docuseal_url"), zugang.geheimnis(zugang.DOCUSEAL_KEY)
    if not url or not key:
        raise NichtEingerichtet("DocuSeal is not configured in Hermes (docuseal_url and %s)" % zugang.DOCUSEAL_KEY)
    return url, key


def _origin(u):
    x = urllib.parse.urlsplit(u)
    return (x.scheme.lower(), (x.hostname or "").lower(), x.port or (443 if x.scheme.lower() == "https" else 80))


class _KeinUmleiten(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None  # a redirect would carry the key elsewhere


OPENER = urllib.request.build_opener(_KeinUmleiten)


def api(pfad, body=None, method=None, roh=False):
    basis, key = _ziel()
    req = urllib.request.Request(basis + "/api" + pfad, data=json.dumps(body).encode() if body is not None else None,
                                 method=method or ("POST" if body is not None else "GET"),
                                 headers={"X-Auth-Token": key, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with OPENER.open(req, timeout=120) as h:
            d = h.read()
            return h.status, (d if roh else json.loads(d or b"null"))
    except urllib.error.HTTPError as e:
        return e.code, zugang.schwaerzen(e.read()[:400].decode("utf-8", "replace"))


def _laden(url):
    basis, key = _ziel()
    kopf = {"X-Auth-Token": key} if _origin(url) == _origin(basis) else {}  # key never to other hosts
    with OPENER.open(urllib.request.Request(url, headers=kopf), timeout=300) as h:
        return h.read()


def _sha_datei(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _stand_pfad(mandant):
    return os.path.join(bp.WURZEL, mandant, "docuseal.json")


def stand_lesen(mandant):
    p = _stand_pfad(mandant)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"vorgaenge": {}}


def stand_speichern(mandant, s):
    p = _stand_pfad(mandant)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".neu", "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(p + ".neu", p)


def vorlagen():
    st, r = api("/templates?limit=100")
    if st != 200:
        raise RuntimeError("DocuSeal templates: %s %s" % (st, str(r)[:200]))
    return [{"id": t["id"], "name": t["name"], "felder": [f["name"] for f in t.get("fields", [])], "rollen": [s["name"] for s in t.get("submitters", [])]} for t in r.get("data", [])]


def senden(mandant, vorlage_id, an, name="", rolle=None, beleg=None, mail=False, betreff=None, werte=None):
    """Richtung 1: Vorgang aus Vorlage anlegen. beleg = Pfad des Originals (sha wird gemerkt, Rueckverfolgung)."""
    st, t = api("/templates/%d" % vorlage_id)
    if st != 200:
        raise RuntimeError("DocuSeal template %d: %s %s" % (vorlage_id, st, str(t)[:200]))
    rolle = rolle or (t.get("submitters") or [{"name": "First Party"}])[0]["name"]
    sub = {"role": rolle, "email": an}
    if name:
        sub["name"] = name
    if werte:
        sub["values"] = werte
    body = {"template_id": vorlage_id, "send_email": bool(mail), "submitters": [sub]}
    if betreff and mail:
        body["message"] = {"subject": betreff, "body": "Bitte unterschreiben: {{template.name}} — {{submitter.link}}"}
    prot = bp.Protokoll(bp.pfad_fuer(mandant))  # Sperre VOR dem Versand: ist das Protokoll besetzt, geht keine Einladung raus
    try:
        return _senden_mit_protokoll(prot, mandant, vorlage_id, t, body, an, name, rolle, beleg, mail)
    finally:
        prot.close()


def _senden_mit_protokoll(prot, mandant, vorlage_id, t, body, an, name, rolle, beleg, mail):
    st, r = api("/submissions", body)
    if st not in (200, 201):
        raise RuntimeError("DocuSeal create submission: %s %s" % (st, str(r)[:200]))
    submitters = r if isinstance(r, list) else r.get("submitters", [r])
    s0 = submitters[0]
    vorgang = s0.get("submission_id") or (s0.get("submission") or {}).get("id")
    if vorgang is None:
        raise RuntimeError("DocuSeal returned no submission id")
    link = s0.get("embed_src") or (_ziel()[0] + "/s/" + s0["slug"] if s0.get("slug") else "")
    beleg_sha = _sha_datei(beleg) if beleg and os.path.isfile(beleg) else ""
    s = stand_lesen(mandant)
    s["vorgaenge"][str(vorgang)] = {"vorlage": vorlage_id, "vorlage_name": t.get("name"), "an": an, "rolle": rolle, "link": link, "beleg": beleg or "", "beleg_sha256": beleg_sha,
                                   "angelegt": bp._jetzt(), "status": "pending", "abgeholt": False, "mail": bool(mail)}
    stand_speichern(mandant, s)
    prot.schreiben("versendet", quelle=beleg or ("docuseal:vorlage/%d" % vorlage_id), ziel="docuseal:vorgang/%s" % vorgang, sha256=beleg_sha,
                   werkzeug="trove:docuseal", mandant=mandant, lauf="docuseal-%s" % vorgang,
                   meldung="Zur Unterschrift: Vorlage '%s' an %s%s (Vorgang %s)%s" % (t.get("name"), an, " (" + name + ")" if name else "", vorgang, "" if mail else "; keine Mail, Link im Stand docuseal.json"),
                   vorgang=vorgang, vorlage=vorlage_id, an=an, mail=bool(mail))  # Signier-Link bewusst nicht ins Protokoll (wirkt wie ein Schluessel, wird nach ERPNext gespiegelt)
    return {"vorgang": vorgang, "link": link, "an": an, "vorlage": t.get("name")}


def abholen(mandant, nur_vorgang=None):
    """Richtung 2: Stand aller bekannten Vorgaenge; fertige Vorgaenge herunterladen und protokollieren."""
    s = stand_lesen(mandant)
    erg = []
    st, alle = api("/submissions?limit=100")
    bekannt = {str(x["id"]): x for x in (alle.get("data", []) if st == 200 else [])}
    # auch Vorgaenge, die nicht ueber Trove angelegt wurden (z. B. in der Web-UI), aufnehmen
    for vid, x in bekannt.items():
        if vid not in s["vorgaenge"]:
            s["vorgaenge"][vid] = {"vorlage": (x.get("template") or {}).get("id"), "vorlage_name": (x.get("template") or {}).get("name"), "an": ", ".join(u.get("email", "") for u in x.get("submitters", [])),
                                  "link": "", "beleg": "", "beleg_sha256": "", "angelegt": x.get("created_at"), "status": x.get("status"), "abgeholt": False, "quelle": "docuseal-ui"}
    prot = bp.Protokoll(bp.pfad_fuer(mandant))
    try:
      for vid, v in sorted(s["vorgaenge"].items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
        if nur_vorgang and str(nur_vorgang) != vid:
            continue
        try:
            _vorgang_abholen(prot, mandant, s, vid, v, erg)
        except Exception as ex:
            erg.append({"vorgang": vid, "fehler": str(ex)[:200]})
        stand_speichern(mandant, s)
    finally:
        prot.close()
    return erg


def _vorgang_abholen(prot, mandant, s, vid, v, erg):
        st, d = api("/submissions/%s" % vid)
        if st != 200:
            erg.append({"vorgang": vid, "fehler": "%s %s" % (st, str(d)[:120])}); return
        status = d.get("status") or ("completed" if all(u.get("status") == "completed" for u in d.get("submitters", [])) else "pending")
        v["status"] = status
        v["unterzeichner"] = [{"email": u.get("email"), "status": u.get("status"), "completed_at": u.get("completed_at")} for u in d.get("submitters", [])]
        z = {"vorgang": vid, "status": status, "an": v.get("an"), "abgeholt": v.get("abgeholt")}
        if status == "completed" and not v.get("abgeholt"):
            ordner = os.path.join(bp.WURZEL, mandant, "unterschrieben", "vorgang_%s" % vid)
            os.makedirs(ordner, exist_ok=True)
            dateien = []
            quellen = [(doc.get("name") or "dokument", doc.get("url")) for doc in d.get("documents", []) if doc.get("url")]
            if d.get("audit_log_url"):
                quellen.append(("audit-log", d["audit_log_url"]))
            for i, (nm, url) in enumerate(quellen, 1):
                nm = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", nm)[:100] or "dokument_%d" % i
                if not nm.lower().endswith(".pdf"):
                    nm += ".pdf"
                p = os.path.join(ordner, "%02d_%s" % (i, nm))  # laufende Nummer: gleiche Namen im Vorgang kollidieren nicht
                if os.path.exists(p):
                    sha = _sha_datei(p); zustand = "schon_da"
                else:
                    daten = _laden(url)
                    with open(p + ".teil", "wb") as f:
                        f.write(daten)
                    sha = _sha_datei(p + ".teil")
                    if sha != hashlib.sha256(daten).hexdigest():
                        os.remove(p + ".teil"); raise RuntimeError("read-back check failed: " + os.path.basename(p))
                    os.replace(p + ".teil", p); zustand = "neu"
                dateien.append({"datei": p, "sha256": sha, "zustand": zustand})
                if not v.get("abgeholt"):  # Ereignis auch fuer Dateien, die ein abgebrochener Lauf schon abgelegt hatte
                    prot.schreiben("unterschrieben", quelle="docuseal:vorgang/%s/%s" % (vid, nm), ziel=p, sha256=sha, bytes_=os.path.getsize(p),
                                 werkzeug="trove:docuseal", mandant=mandant, lauf="docuseal-%s" % vid,
                                 meldung="Unterschrieben zurueck: %s (%s) von %s; Original sha %s" % (nm, v.get("vorlage_name"), v.get("an"), (v.get("beleg_sha256") or "-")[:16]),
                                 vorgang=int(vid), original=v.get("beleg") or "", original_sha256=v.get("beleg_sha256") or "", unterzeichner=v["unterzeichner"])
            v["abgeholt"] = True; v["dateien"] = dateien; v["abgeholt_am"] = bp._jetzt()
            z["dateien"] = dateien
        erg.append(z)


def _selbsttest():
    """Inactive without configuration (no network); with a mocked Hermes the key goes only to the configured origin."""
    import http.server, tempfile, threading
    alt = zugang.geheimnis_quelle, zugang.einstellungen_quelle, bp.WURZEL
    try:
        zugang.geheimnis_quelle = lambda n: None
        zugang.einstellungen_quelle = lambda: {}
        assert eingerichtet() == (False, ["docuseal_url", zugang.DOCUSEAL_KEY])
        try:
            vorlagen()
            raise AssertionError("DocuSeal called without configuration")
        except NichtEingerichtet:
            pass
        gesehen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                gesehen.append((self.path, self.headers.get("X-Auth-Token")))
                b = json.dumps({"data": [{"id": 1, "name": "NDA", "fields": [], "submitters": [{"name": "First Party"}]}]}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            zugang.geheimnis_quelle = lambda n: {zugang.DOCUSEAL_KEY: "ds-key-4711"}.get(n)
            zugang.einstellungen_quelle = lambda: {"docuseal_url": "http://127.0.0.1:%d" % srv.server_address[1]}
            assert eingerichtet() == (True, [])
            assert vorlagen()[0]["name"] == "NDA" and gesehen[-1] == ("/api/templates?limit=100", "ds-key-4711"), gesehen
            _laden("http://127.0.0.1:%d/file" % srv.server_address[1])
            assert gesehen[-1][1] == "ds-key-4711"
            try:
                _laden("http://127.0.0.1:9/elsewhere")  # other origin: no key (and nothing listens there)
            except OSError:
                pass
            with tempfile.TemporaryDirectory() as w:
                bp.WURZEL = w
                assert stand_lesen("t") == {"vorgaenge": {}}
        finally:
            srv.shutdown()
    finally:
        zugang.geheimnis_quelle, zugang.einstellungen_quelle, bp.WURZEL = alt


if __name__ == "__main__":
    _selbsttest()
    print("docuseal selftest ok")
