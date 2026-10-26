"""Trove F7 — Unterschreiben und versenden in beide Richtungen ueber DocuSeal (lokal, http://127.0.0.1:8091).

Richtung 1 (wir senden): aus einer DocuSeal-Vorlage einen Vorgang anlegen (Unterzeichner, optional Mail durch DocuSeal), Link
  ins Belegprotokoll ("versendet", Bezug zum Beleg ueber sha256 des Originals).
Richtung 2 (wir empfangen): Stand aller Vorgaenge abfragen; fertige Vorgaenge: unterschriebene PDFs + Audit-Log holen nach
  <TROVE_DATEN>\\<mandant>\\unterschrieben\\<vorgang>\\, Pruefsumme, Ereignis "unterschrieben" (rueckwaerts bis zum Original).
Grenze der freien Edition (gemessen 18.10.2026): Vorlagen/Dokumente per API hochladen = Pro. Vorlagen entstehen deshalb in der
  DocuSeal-Web-UI (Mensch); alles Weitere laeuft per API. API-Schluessel: Zeile "api-key:" in
  <TROVE_DATEN>\\_geheim\\docuseal_erstanmeldung.txt (nur Pfad, nie Inhalt im Repo).
Stand je Mandant: <TROVE_DATEN>\\<mandant>\\docuseal.json (Vorgang -> Beleg-sha, Status, abgeholt).
"""
import argparse, hashlib, json, os, re, sys, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "plugin", "dashboard"))  # Trove core modules
import belegprotokoll as bp

DOCUSEAL = os.environ.get("TROVE_DOCUSEAL", "http://127.0.0.1:8091")
SCHLUESSEL_DATEI = os.environ.get("TROVE_DOCUSEAL_SCHLUESSEL", os.path.join(bp.WURZEL, "_geheim", "docuseal_erstanmeldung.txt"))


def _schluessel():
    m = re.search(r"api-key:\s*(\S+)", open(SCHLUESSEL_DATEI, encoding="utf-8").read())
    if not m:
        raise RuntimeError("kein api-key in " + SCHLUESSEL_DATEI)
    return m.group(1)


def api(pfad, body=None, method=None, roh=False):
    req = urllib.request.Request(DOCUSEAL + "/api" + pfad, data=json.dumps(body).encode() if body is not None else None,
                                 method=method or ("POST" if body is not None else "GET"),
                                 headers={"X-Auth-Token": _schluessel(), "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as h:
            d = h.read()
            return h.status, (d if roh else json.loads(d or b"null"))
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:400].decode("utf-8", "replace")


def _laden(url):
    req = urllib.request.Request(url, headers=({"X-Auth-Token": _schluessel()} if url.startswith(DOCUSEAL) else {}))  # Schluessel nie an Fremdziele
    with urllib.request.urlopen(req, timeout=300) as h:
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
        raise RuntimeError("DocuSeal templates: %s %s" % (st, r))
    return [{"id": t["id"], "name": t["name"], "felder": [f["name"] for f in t.get("fields", [])], "rollen": [s["name"] for s in t.get("submitters", [])]} for t in r.get("data", [])]


def senden(mandant, vorlage_id, an, name="", rolle=None, beleg=None, mail=False, betreff=None, werte=None):
    """Richtung 1: Vorgang aus Vorlage anlegen. beleg = Pfad des Originals (sha wird gemerkt, Rueckverfolgung)."""
    st, t = api("/templates/%d" % vorlage_id)
    if st != 200:
        raise RuntimeError("Vorlage %d: %s %s" % (vorlage_id, st, t))
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
        raise RuntimeError("Vorgang anlegen: %s %s" % (st, r))
    submitters = r if isinstance(r, list) else r.get("submitters", [r])
    s0 = submitters[0]
    vorgang = s0.get("submission_id") or (s0.get("submission") or {}).get("id")
    if vorgang is None:
        raise RuntimeError("DocuSeal lieferte keine Vorgangsnummer: " + str(r)[:200])
    link = s0.get("embed_src") or (DOCUSEAL + "/s/" + s0["slug"] if s0.get("slug") else "")
    beleg_sha = _sha_datei(beleg) if beleg and os.path.isfile(beleg) else ""
    s = stand_lesen(mandant)
    s["vorgaenge"][str(vorgang)] = {"vorlage": vorlage_id, "vorlage_name": t.get("name"), "an": an, "rolle": rolle, "link": link, "beleg": beleg or "", "beleg_sha256": beleg_sha,
                                   "angelegt": bp._jetzt(), "status": "pending", "abgeholt": False, "mail": bool(mail)}
    stand_speichern(mandant, s)
    prot.schreiben("versendet", quelle=beleg or ("docuseal:vorlage/%d" % vorlage_id), ziel="docuseal:vorgang/%s" % vorgang, sha256=beleg_sha,
                   werkzeug="docuseal.py", mandant=mandant, lauf="docuseal-%s" % vorgang,
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
                        os.remove(p + ".teil"); raise RuntimeError("Rueckleseprobe fehlgeschlagen: " + p)
                    os.replace(p + ".teil", p); zustand = "neu"
                dateien.append({"datei": p, "sha256": sha, "zustand": zustand})
                if not v.get("abgeholt"):  # Ereignis auch fuer Dateien, die ein abgebrochener Lauf schon abgelegt hatte
                    prot.schreiben("unterschrieben", quelle="docuseal:vorgang/%s/%s" % (vid, nm), ziel=p, sha256=sha, bytes_=os.path.getsize(p),
                                 werkzeug="docuseal.py", mandant=mandant, lauf="docuseal-%s" % vid,
                                 meldung="Unterschrieben zurueck: %s (%s) von %s; Original sha %s" % (nm, v.get("vorlage_name"), v.get("an"), (v.get("beleg_sha256") or "-")[:16]),
                                 vorgang=int(vid), original=v.get("beleg") or "", original_sha256=v.get("beleg_sha256") or "", unterzeichner=v["unterzeichner"])
            v["abgeholt"] = True; v["dateien"] = dateien; v["abgeholt_am"] = bp._jetzt()
            z["dateien"] = dateien
        erg.append(z)


def main():
    ap = argparse.ArgumentParser(description="Trove F7 DocuSeal")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("vorlagen")
    s = sub.add_parser("senden"); s.add_argument("--mandant", default="haus"); s.add_argument("--vorlage", type=int, required=True); s.add_argument("--an", required=True)
    s.add_argument("--name", default=""); s.add_argument("--rolle"); s.add_argument("--beleg", help="Original (Pfad) fuer die Rueckverfolgung"); s.add_argument("--mail", action="store_true", help="DocuSeal verschickt die Einladung per E-Mail")
    s.add_argument("--betreff")
    s = sub.add_parser("abholen"); s.add_argument("--mandant", default="haus"); s.add_argument("--vorgang")
    s = sub.add_parser("stand"); s.add_argument("--mandant", default="haus")
    a = ap.parse_args()
    if a.cmd == "vorlagen":
        print(json.dumps(vorlagen(), ensure_ascii=False, indent=1))
    elif a.cmd == "senden":
        print(json.dumps(senden(a.mandant, a.vorlage, a.an, a.name, a.rolle, a.beleg, a.mail, a.betreff), ensure_ascii=False))
    elif a.cmd == "abholen":
        print(json.dumps(abholen(a.mandant, a.vorgang), ensure_ascii=False, indent=1))
    elif a.cmd == "stand":
        print(json.dumps(stand_lesen(a.mandant), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
