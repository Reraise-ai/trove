"""Backend checks used by tools/test_backend.py (kept separate so each feature commit can extend them)."""
import os


def run(c, u, tmp, mod):
    quelle = os.path.join(tmp, "inbox")
    os.makedirs(quelle)
    with open(os.path.join(quelle, "note.txt"), "w", encoding="utf-8") as f:
        f.write("hello trove")
    r = c.post(u + "/quellen", json={"art": "ordner", "pfad": quelle})
    assert r.status_code == 200, r.text
    q = r.json()
    r = c.get(u + "/liste", params={"quelle": q["id"]})
    assert r.status_code == 200 and [e["name"] for e in r.json()["eintraege"]] == ["note.txt"], r.text
    r = c.get(u + "/vorschau", params={"quelle": q["id"], "pfad": "note.txt"})
    assert r.status_code == 200 and r.json()["inhalt"] == "hello trove", r.text
    # --- per-folder write permission: read-only unless a source is explicitly marked writable ---
    assert q["nur_lesen"] is True, q  # fail-safe default
    ziel = os.path.join(tmp, "archive")
    os.makedirs(ziel)
    z = c.post(u + "/quellen", json={"art": "ordner", "pfad": ziel}).json()
    assert z["nur_lesen"] is True, z
    r = c.post(u + "/quellen", json={"art": "rclone", "pfad": "x:y"})
    assert r.status_code == 400, r.text  # no cloud/remote sources: files arrive in local folders via the agents
    aktion = {"art": "kopieren", "von": {"quelle": q["id"], "pfad": ""}, "nach": {"quelle": z["id"], "pfad": ""}, "namen": ["note.txt"]}
    r = c.post(u + "/aktion", json=aktion)
    assert r.status_code == 403 and not os.listdir(ziel), r.text  # target read-only -> nothing written
    r = c.post(u + "/quellen/" + z["id"] + "/schreibbar", json={"nur_lesen": False})
    assert r.status_code == 200 and r.json()["nur_lesen"] is False, r.text
    r = c.post(u + "/aktion", json=aktion)
    assert r.status_code == 200 and os.listdir(ziel) == ["note.txt"] and os.path.exists(os.path.join(quelle, "note.txt")), r.text
    r = c.post(u + "/aktion", json={**aktion, "art": "verschieben", "namen": ["note.txt"], "nach": {"quelle": z["id"], "pfad": ""}})
    assert r.status_code == 403 and os.path.exists(os.path.join(quelle, "note.txt")), r.text  # move deletes the original: source must be writable too
    r = c.post(u + "/quellen/" + z["id"] + "/schreibbar", json={})
    assert r.json()["nur_lesen"] is True  # anything but an explicit false means read-only
    # --- Into knowledge: no hidden watch-list entry unless asked for ---
    import konfig
    wiss = {"quelle": q["id"], "pfad": "", "sammlung": "t1", "karten": True, "ohne_embed": True}
    r = c.post(u + "/wissen", json=wiss)
    assert r.status_code == 200 and r.json()["nachgefuehrt"] is False and not konfig.lesen().get("ueberwacht"), r.text
    r = c.post(u + "/wissen", json={**wiss, "nachfuehren": True})
    assert r.status_code == 200 and r.json()["nachgefuehrt"] is True and len(konfig.lesen()["ueberwacht"]) == 1, r.text
    run_integrationen(c, u, tmp, mod)


def run_integrationen(c, u, tmp, mod):
    """Optional integrations: inactive without Hermes configuration (no network at all), Reraise fail-closed (link only),
    secrets come only from the Hermes mock and never appear in responses or log lines."""
    import json
    import logging
    import sys
    import zugang
    import reraise_hochladen as rh
    assert os.path.dirname(zugang.__file__) == os.path.dirname(mod.__file__)  # integrations ship inside the plugin copy

    class KeinNetz:
        def open(self, *a, **k):
            raise AssertionError("network used without configuration")
    rh.OPENER = KeinNetz()
    zugang.anfrage_quelle = lambda *a: (_ for _ in ()).throw(AssertionError("Gmail request without configuration"))
    log = []

    class Fang(logging.Handler):
        def emit(self, record):
            log.append(record.getMessage())
    logging.getLogger("trove").addHandler(Fang())
    # 1) nothing configured (outside Hermes): everything inactive, every action refused before any request
    s = c.get(u + "/integrationen").json()
    assert not s["docuseal"]["active"] and not s["erpnext"]["active"] and not s["reraise"]["active"], s
    assert [b["active"] for b in s["mail"]["mailboxes"]] == [False, False] and s["reraise"]["link"] == "https://reraise.ai", s
    assert "TROVE_RERAISE_KEY" not in json.dumps(s["reraise"]), s  # the Reraise row offers only the link
    for meth, pfad, body in (("get", "/integrationen/docuseal/vorlagen", None), ("post", "/integrationen/docuseal/abholen", {}),
                             ("post", "/integrationen/erpnext/spiegeln", {}), ("post", "/integrationen/erpnext/doctypes", {}),
                             ("post", "/integrationen/mail/abholen", {"postfach": "imap"}), ("post", "/integrationen/mail/abholen", {"postfach": "gmail"}),
                             ("post", "/integrationen/reraise/probe", {"quelle": "x"}),
                             ("post", "/integrationen/reraise/senden", {"quelle": "x", "bestaetigt": ["a" * 64]})):
        r = getattr(c, meth)(u + pfad, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (pfad, r.status_code, r.text)
    # 2) Hermes mock: secrets and settings as Hermes would hand them out (services at 127.0.0.1:9 = nothing listens)
    geheim = {"TROVE_DOCUSEAL_API_KEY": "ds-SECRET-1111", "TROVE_ERPNEXT_API_KEY": "erp-KEY-2222", "TROVE_ERPNEXT_API_SECRET": "erp-SECRET-3333",
              "TROVE_IMAP_PASSWORD": "imap-SECRET-4444", "TROVE_RERAISE_KEY": "rr-SECRET-5555"}
    zugang.geheimnis_quelle = geheim.get
    zugang.einstellungen_quelle = lambda: {"docuseal_url": "http://127.0.0.1:9", "erpnext_url": "http://127.0.0.1:9", "imap_host": "127.0.0.1",
                                           "imap_user": "me", "imap_port": 9}
    try:
        s = c.get(u + "/integrationen").json()
        assert s["docuseal"]["active"] and s["erpnext"]["active"] and s["reraise"]["active"], s
        assert [b["active"] for b in s["mail"]["mailboxes"]] == [True, False], s  # gmail stays off without its OAuth client
        texte = [json.dumps(s)]
        for meth, pfad, body in (("get", "/integrationen/docuseal/vorlagen", None), ("post", "/integrationen/erpnext/spiegeln", {}),
                                 ("post", "/integrationen/mail/abholen", {"postfach": "imap"})):
            r = getattr(c, meth)(u + pfad, **({"json": body} if body is not None else {}))
            assert r.status_code in (403, 502), (pfad, r.status_code, r.text)  # nothing listens on port 9
            texte.append(r.text)
        # Reraise with key: preview, then upload of the confirmed card (network mocked)
        ein = os.path.join(tmp, "oeffentlich")
        os.makedirs(ein)
        with open(os.path.join(ein, "beleg.txt"), "w", encoding="utf-8") as f:
            f.write("Rechnung 4711 Summe 10,00 EUR")
        q = c.post(u + "/quellen", json={"art": "ordner", "pfad": ein}).json()
        r = c.post(u + "/wissen", json={"quelle": q["id"], "pfad": "", "sammlung": "oe1", "sichtbarkeit": "oeffentlich", "zugehoerigkeit": "Example Ltd", "ohne_embed": True})
        assert r.status_code == 200, r.text
        r = c.post(u + "/integrationen/reraise/probe", json={"quelle": q["id"], "pfad": "", "sammlung": "oe1"})
        assert r.status_code == 200 and r.json()["gebaut"] == 1, r.text
        e = r.json()["eintraege"][0]
        texte.append(r.text)
        rh.OPENER = netz = rh._Netz({"cards": []}, {"ok": True, "card": {"id": "c1", "no": 1}}, mandant=mod.MANDANT)
        r = c.post(u + "/integrationen/reraise/senden", json={"quelle": q["id"], "pfad": "", "sammlung": "oe1",
                                                             "bestaetigt": [e["sha256"] + ":" + e["karte_hash"]]})
        assert r.status_code == 200 and r.json()["gesendet"] == 1, r.text
        assert [a[0] for a in netz.anfragen] == ["GET", "POST"] and netz.anfragen[1][1] == "https://reraise.ai/api/x/cards", netz.anfragen
        texte.append(r.text)
        alles = "\n".join(texte + log)
        for w in geheim.values():
            assert w not in alles, "secret leaked"
        with open(os.path.join(mod.TROVE_DATEN, mod.MANDANT, "belegprotokoll.jsonl"), encoding="utf-8") as f:
            prot = f.read()
        assert not any(w in prot for w in geheim.values()), "secret in receipt log"
    finally:
        zugang.geheimnis_quelle = zugang.einstellungen_quelle = zugang.anfrage_quelle = None
        rh.OPENER = KeinNetz()
    r = c.post(u + "/integrationen/reraise/probe", json={"quelle": q["id"], "pfad": "", "sammlung": "oe1"})
    assert r.status_code == 403, r.text  # key gone -> fail closed again
    print("integrations ok (%d log lines checked)" % len(log), file=sys.stderr)
