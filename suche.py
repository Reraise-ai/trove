"""Trove F15 — eine Suche ueber beide Welten: "liegt bei dir unter ..." / "liegt bei uns unter ...".

Quellen (alle lokal, kein Modell):
  register    C:\\datenlager\\trove\\<mandant>\\register.db (Tabelle datei: ort, pfad, name, sha256) — Welt A + B, was F1/F2 aufgenommen hat
  altindex    C:\\datenlager\\_eingang\\datei-zentrale\\reg_z_2026-10-17.db (zarmina-Index, 62.368 Dateien)
  protokoll   Belegprotokoll (Pfade, sha256, Meldungen — darunter Mail-Betreffe, Lese-Ergebnisse, Unterschriften)
  erpnext     Trove Beleg (Dateiname, Rechnungsnummer, Aussteller, letzter Ort) + Kundenakten (Customer, Supplier, Purchase/Sales Invoice)
  paperless   Welt-A-Archiv (A-11): /api/documents/?query=  — Token C:\\datenlager\\platform\\paperless\\api_token.txt (nur Pfad)
"bei uns" = auf dem Server (C:\\...), Paperless, ERPNext, DocuSeal; "im Postfach" = mail:...; "bei dir" = fremde Maschine oder Remote (/home/..., UNC, rclone).
"""
import argparse, json, os, re, sqlite3, sys, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import belegprotokoll as bp

ALTINDEX = os.environ.get("TROVE_REGISTER_DB", r"C:\datenlager\_eingang\datei-zentrale\reg_z_2026-10-17.db")
PAPERLESS = os.environ.get("TROVE_PAPERLESS", "http://127.0.0.1:8010")
PAPERLESS_TOKEN = os.environ.get("TROVE_PAPERLESS_TOKEN", r"C:\datenlager\platform\paperless\api_token.txt")


def wo(pfad):
    """bei uns = auf dem Server (C:\\...), Paperless, ERPNext, DocuSeal; im Postfach = mail:...; bei dir = fremde Maschine/Remote (/home/..., \\\\server, rclone-Name:)."""
    p = (pfad or "").strip().casefold()
    if p.startswith("mail:"):
        return "im Postfach"
    if re.match(r"^[a-z]:[\\/]", p) or p.startswith(("paperless:", "erpnext:", "docuseal:")):
        return "bei uns"
    return "bei dir"


def _like(q):
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def register(mandant, q, n=30):
    out = []
    for db, quelle in ((os.path.join(bp.WURZEL, mandant, "register.db"), "register"), (ALTINDEX, "altindex")):
        if not os.path.exists(db):
            continue
        c = sqlite3.connect(db)
        try:
            for ort, pfad, name, groesse, sha in c.execute("select ort,pfad,name,groesse,sha256 from datei where name like ? escape '\\' or pfad like ? escape '\\' or sha256 = ? limit ?", (_like(q), _like(q), q, n)):
                voll = pfad if (pfad or "").lower().startswith(("c:", "/", "\\\\")) else os.path.join(ort or "", pfad or "")
                out.append({"quelle": quelle, "ort": ort, "pfad": voll, "name": name, "bytes": groesse, "sha256": sha, "wo": wo(voll)})
        finally:
            c.close()
    return out


def protokoll(mandant, q, n=40):
    ql = q.casefold()
    out = []
    if not os.path.exists(bp.pfad_fuer(mandant)):
        return out
    for e in bp.eintraege(bp.pfad_fuer(mandant)):
        if ql in (e.get("quelle") or "").casefold() or ql in (e.get("ziel") or "").casefold() or ql in (e.get("meldung") or "").casefold() or (len(q) >= 8 and (e.get("sha256") or "").startswith(ql)):
            out.append({"nr": e["nr"], "zeit": e["zeit"], "ereignis": e["ereignis"], "quelle": e.get("quelle"), "ziel": e.get("ziel"), "sha256": e.get("sha256"), "meldung": (e.get("meldung") or "")[:200],
                        "wo": wo(e.get("ziel") or e.get("quelle"))})
    return out[-n:][::-1]


def erpnext(q, n=20):
    import spiegel_erpnext as sp
    out = {"belege": [], "kunden": []}
    try:
        st, r = sp.api("/api/method/frappe.client.get_list", {"doctype": sp.DT_BELEG, "or_filters": [["dateiname", "like", _like(q)], ["rechnungsnummer", "like", _like(q)], ["aussteller_kennung", "like", _like(q)], ["letzter_ort", "like", _like(q)], ["sha256", "=", q]],
                                                              "fields": ["name", "dateiname", "letzter_ort", "belegdatum", "brutto", "waehrung", "rechnungsnummer", "aussteller_kennung", "verprobung"], "limit_page_length": n})
        if st == 200:
            out["belege"] = [{**x, "wo": wo(x.get("letzter_ort"))} for x in r.get("message", [])]
        for dt, felder, such in (("Customer", ["name", "customer_name", "tax_id"], ["customer_name", "tax_id", "name"]), ("Supplier", ["name", "supplier_name", "tax_id"], ["supplier_name", "tax_id", "name"]),
                                 ("Purchase Invoice", ["name", "supplier_name", "bill_no", "posting_date", "grand_total", "currency"], ["supplier_name", "bill_no", "name"]),
                                 ("Sales Invoice", ["name", "customer_name", "posting_date", "grand_total", "currency"], ["customer_name", "name"])):
            st, r = sp.api("/api/method/frappe.client.get_list", {"doctype": dt, "or_filters": [[f, "like", _like(q)] for f in such], "fields": felder, "limit_page_length": n})
            if st == 200:
                for x in r.get("message", []):
                    out["kunden"].append({"doctype": dt, **x, "wo": "bei uns", "link": sp.ERP + "/app/" + dt.lower().replace(" ", "-") + "/" + urllib.parse.quote(x["name"])})
    except Exception as ex:
        out["fehler"] = str(ex)[:200]
    return out


def paperless(q, n=20):
    if not os.path.exists(PAPERLESS_TOKEN):
        return {"dokumente": [], "fehler": "kein Token: " + PAPERLESS_TOKEN}
    tok = open(PAPERLESS_TOKEN, encoding="utf-8").read().strip()
    try:
        req = urllib.request.Request(PAPERLESS + "/api/documents/?" + urllib.parse.urlencode({"query": q, "page_size": n, "fields": "id,title,created,archive_serial_number,original_file_name"}), headers={"Authorization": "Token " + tok})
        with urllib.request.urlopen(req, timeout=30) as h:
            r = json.loads(h.read())
        return {"dokumente": [{"id": d["id"], "titel": d.get("title"), "erstellt": d.get("created"), "asn": d.get("archive_serial_number"), "datei": d.get("original_file_name"), "wo": "bei uns", "link": PAPERLESS + "/documents/%d/details" % d["id"]} for d in r.get("results", [])], "gesamt": r.get("count")}
    except Exception as ex:
        return {"dokumente": [], "fehler": str(ex)[:200]}


def suchen(q, mandant="haus", n=30):
    q = (q or "").strip()
    if len(q) < 2:
        return {"frage": q, "hinweis": "mindestens 2 Zeichen"}
    reg = register(mandant, q, n)
    prot = protokoll(mandant, q, n)
    erp = erpnext(q, n)
    pap = paperless(q, n)
    orte = {}
    for r in reg:
        orte.setdefault(r["wo"], set()).add(r["pfad"])
    for p in prot:
        for k in ("quelle", "ziel"):
            if p.get(k) and q.casefold() in p[k].casefold():
                orte.setdefault(wo(p[k]), set()).add(p[k])
    for b in erp.get("belege", []):
        if b.get("letzter_ort"):
            orte.setdefault(b["wo"], set()).add(b["letzter_ort"])
    for d in pap.get("dokumente", []):
        orte.setdefault("bei uns", set()).add("paperless:%d %s" % (d["id"], d.get("titel") or ""))
    return {"frage": q, "mandant": mandant, "liegt": {k: sorted(v)[:n] for k, v in orte.items()}, "register": reg, "protokoll": prot, "erpnext": erp, "paperless": pap}


def main():
    ap = argparse.ArgumentParser(description="Trove F15 Suche ueber beide Welten")
    ap.add_argument("frage")
    ap.add_argument("--mandant", default="haus")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = suchen(a.frage, a.mandant)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1)); return
    for k, v in r.get("liegt", {}).items():
        print("liegt %s unter:" % k)
        for p in v:
            print("   ", p)
    print("Register %d, Protokoll %d, ERPNext Belege %d / Kunden %d, Paperless %d" % (len(r["register"]), len(r["protokoll"]), len(r["erpnext"].get("belege", [])), len(r["erpnext"].get("kunden", [])), len(r["paperless"].get("dokumente", []))))
    for k in ("erpnext", "paperless"):
        if r[k].get("fehler"):
            print(k, "Fehler:", r[k]["fehler"])


if __name__ == "__main__":
    main()
