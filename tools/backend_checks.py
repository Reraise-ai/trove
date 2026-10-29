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
    desk_basics(c, u, tmp, mod)
    run_integrationen(c, u, tmp, mod)


def desk_basics(c, u, tmp, mod):
    """Rename and new folder: writable sources only, never overwrite, no traversal, every change in the receipt log."""
    import belegprotokoll
    wurzel = os.path.join(tmp, "desk")
    os.makedirs(os.path.join(wurzel, "sub"))
    for n, inhalt in (("a.txt", "A"), ("b.txt", "B")):
        with open(os.path.join(wurzel, n), "w", encoding="utf-8") as f:
            f.write(inhalt)
    q = c.post(u + "/quellen", json={"art": "ordner", "pfad": wurzel}).json()
    ren = lambda **k: c.post(u + "/umbenennen", json={"quelle": q["id"], "pfad": "", **k})
    neu = lambda **k: c.post(u + "/ordner-neu", json={"quelle": q["id"], "pfad": "", **k})
    # read-only (default): refused, nothing changes
    r = ren(name="a.txt", neu="z.txt")
    assert r.status_code == 403 and os.path.exists(os.path.join(wurzel, "a.txt")), r.text
    r = neu(name="fresh")
    assert r.status_code == 403 and not os.path.exists(os.path.join(wurzel, "fresh")), r.text
    assert c.post(u + "/quellen/" + q["id"] + "/schreibbar", json={"nur_lesen": False}).status_code == 200

    def log_text():
        mand = c.get(u + "/status").json()["mandant"]
        with open(belegprotokoll.pfad_fuer(mand), encoding="utf-8") as f:
            return f.read()
    # happy path: rename file and folder, create folder (also in a subfolder)
    r = ren(name="a.txt", neu="a2.txt")
    assert r.status_code == 200 and os.path.exists(os.path.join(wurzel, "a2.txt")) and not os.path.exists(os.path.join(wurzel, "a.txt")), r.text
    assert ren(name="sub", neu="sub2").status_code == 200 and os.path.isdir(os.path.join(wurzel, "sub2"))
    assert neu(name="fresh").status_code == 200 and os.path.isdir(os.path.join(wurzel, "fresh"))
    r = c.post(u + "/ordner-neu", json={"quelle": q["id"], "pfad": "sub2", "name": "inner"})
    assert r.status_code == 200 and os.path.isdir(os.path.join(wurzel, "sub2", "inner")), r.text
    # case-only rename works
    assert ren(name="b.txt", neu="B.txt").status_code == 200 and "B.txt" in os.listdir(wurzel)
    # never overwrite
    r = ren(name="a2.txt", neu="B.txt")
    assert r.status_code == 409, r.text
    with open(os.path.join(wurzel, "B.txt"), encoding="utf-8") as f:
        assert f.read() == "B"
    assert os.path.exists(os.path.join(wurzel, "a2.txt"))
    assert neu(name="fresh").status_code == 409 and neu(name="B.txt").status_code == 409
    assert ren(name="missing.txt", neu="x.txt").status_code == 404
    assert ren(name="a2.txt", neu="a2.txt").status_code == 400
    # traversal and invalid names
    for schlecht in ("../evil", "x/y", "x\\y", "..", ".", "", "/abs", "C:evil", "name.", "name ", " name", "CON", "nul.txt", "Com1.log", "a:b", "a*b", "a\x00b", "a" * 300):
        assert ren(name="a2.txt", neu=schlecht).status_code == 400, repr(schlecht)
        assert neu(name=schlecht).status_code == 400, repr(schlecht)
    assert ren(name="../a2.txt", neu="ok.txt").status_code == 400
    assert c.post(u + "/umbenennen", json={"quelle": q["id"], "pfad": "../..", "name": "a2.txt", "neu": "ok.txt"}).status_code == 400
    assert c.post(u + "/ordner-neu", json={"quelle": q["id"], "pfad": "../..", "name": "ok"}).status_code == 400
    assert sorted(os.listdir(wurzel)) == ["B.txt", "a2.txt", "fresh", "sub2"], os.listdir(wurzel)
    assert not os.path.exists(os.path.join(tmp, "evil"))
    # receipt log has every change
    prot = log_text()
    assert prot.count('"umbenannt"') >= 3 and "a.txt" in prot and "a2.txt" in prot and "folder created: fresh" in prot, prot[-600:]
    mand = c.get(u + "/status").json()["mandant"]
    assert belegprotokoll.pruefen(belegprotokoll.pfad_fuer(mand))["kette"] == "ok"  # chain still intact
    # race: the target appears after the existence check (simulated by hiding it from lexists) -> still no overwrite
    for was, ziel_name in (("file", "race.txt"), ("dir", "racedir")):
        quelle_name = "src_" + was
        pfad_q = os.path.join(wurzel, quelle_name)
        if was == "file":
            with open(pfad_q, "w", encoding="utf-8") as f:
                f.write("SOURCE")
            with open(os.path.join(wurzel, ziel_name), "w", encoding="utf-8") as f:
                f.write("TARGET")
        else:
            os.makedirs(pfad_q)
            os.makedirs(os.path.join(wurzel, ziel_name))
            with open(os.path.join(wurzel, ziel_name, "keep.txt"), "w", encoding="utf-8") as f:
                f.write("TARGET")
        echt = os.path.lexists
        ziel_voll = os.path.join(wurzel, ziel_name)
        os.path.lexists = lambda p, _e=echt: False if os.path.normpath(p) == os.path.normpath(ziel_voll) else _e(p)
        try:
            r = ren(name=quelle_name, neu=ziel_name)
        finally:
            os.path.lexists = echt
        assert r.status_code == 409, (was, r.status_code, r.text)
        assert os.path.exists(pfad_q), was
        if was == "file":
            with open(ziel_voll, encoding="utf-8") as f:
                assert f.read() == "TARGET"
        else:
            assert os.listdir(ziel_voll) == ["keep.txt"]
    # the helper itself: link / renameat2 path really renames, and the fallback path is exercised too
    h = os.path.join(wurzel, "h1")
    with open(h, "w", encoding="utf-8") as f:
        f.write("H")
    assert mod._umbenennen_ohne_ueberschreiben(h, h + "x") in ("link", "renameat2", "check+rename", "rename") and os.path.exists(h + "x") and not os.path.exists(h)
    echt_link = os.link
    def kein_link(*a, **k):
        raise OSError(18, "cross-device")
    os.link = kein_link
    try:
        assert mod._umbenennen_ohne_ueberschreiben(h + "x", h + "y") == "check+rename" and os.path.exists(h + "y")
        with open(h + "z", "w", encoding="utf-8") as f:
            f.write("Z")
        try:
            mod._umbenennen_ohne_ueberschreiben(h + "y", h + "z")
            assert False, "fallback must refuse an existing target"
        except FileExistsError:
            pass
    finally:
        os.link = echt_link
    # --- review fixes: case-only rename must be the SAME entry; surrogates; busy log; sources/watched folders ---
    cs = not os.path.exists(os.path.join(wurzel, "B.TXT"))  # B.txt exists: B.TXT visible only on case-insensitive filesystems
    if cs:
        with open(os.path.join(wurzel, "x.txt"), "w", encoding="utf-8") as f:
            f.write("X")
        os.symlink("x.txt", os.path.join(wurzel, "X.txt"))
        r = ren(name="x.txt", neu="X.txt")
        assert r.status_code == 409 and os.path.islink(os.path.join(wurzel, "X.txt")) and os.path.isfile(os.path.join(wurzel, "x.txt")), r.text  # symlink twin kept
        with open(os.path.join(wurzel, "h.txt"), "w", encoding="utf-8") as f:
            f.write("H")
        os.link(os.path.join(wurzel, "h.txt"), os.path.join(wurzel, "H.txt"))
        n_vorher = log_text().count('"umbenannt"')
        r = ren(name="h.txt", neu="H.txt")
        assert r.status_code == 409 and log_text().count('"umbenannt"') == n_vorher, r.text  # hard-link twin: nothing logged as renamed
        os.symlink("nowhere", os.path.join(wurzel, "D.txt"))
        with open(os.path.join(wurzel, "d.txt"), "w", encoding="utf-8") as f:
            f.write("D")
        r = ren(name="d.txt", neu="D.txt")
        assert r.status_code in (400, 409), r.text  # dangling symlink twin: clean refusal, no 500
        assert os.path.islink(os.path.join(wurzel, "D.txt")) and os.path.isfile(os.path.join(wurzel, "d.txt"))
        # a genuine case-only rename still works
        with open(os.path.join(wurzel, "solo.txt"), "w", encoding="utf-8") as f:
            f.write("S")
        assert ren(name="solo.txt", neu="Solo.txt").status_code == 200 and "Solo.txt" in os.listdir(wurzel) and "solo.txt" not in os.listdir(wurzel)
    # lone surrogate -> 400, not 500 (raw JSON body, the client cannot encode it)
    for body in ({"quelle": q["id"], "pfad": "", "name": "a2.txt", "neu": "\udc80x"}, {"quelle": q["id"], "pfad": "", "name": "\udc80x", "neu": "ok"}):
        r = c.post(u + "/umbenennen", content=__import__("json").dumps(body), headers={"content-type": "application/json"})
        assert r.status_code == 400, r.text
    r = c.post(u + "/ordner-neu", content=__import__("json").dumps({"quelle": q["id"], "pfad": "", "name": "\udc80x"}), headers={"content-type": "application/json"})
    assert r.status_code == 400, r.text
    # busy receipt log -> 503 with a clear message, nothing changed
    import belegprotokoll as bpm
    echt_pw = mod.gm.prot_mit_wiederholung
    def gesperrt(*a, **k):
        raise bpm.ProtokollGesperrt("locked")
    mod.gm.prot_mit_wiederholung = gesperrt
    try:
        r1 = ren(name="a2.txt", neu="busy.txt")
        r2 = neu(name="busyfolder")
    finally:
        mod.gm.prot_mit_wiederholung = echt_pw
    assert r1.status_code == 503 and r2.status_code == 503 and "busy" in r1.text, (r1.text, r2.text)
    assert os.path.exists(os.path.join(wurzel, "a2.txt")) and not os.path.exists(os.path.join(wurzel, "busyfolder"))
    # a folder that is itself a registered source, or watched by Into knowledge, cannot be renamed
    os.makedirs(os.path.join(wurzel, "asrc", "deep"))
    qs = c.post(u + "/quellen", json={"art": "ordner", "pfad": os.path.join(wurzel, "asrc", "deep")}).json()
    r = ren(name="asrc", neu="asrc2")
    assert r.status_code == 409 and "source" in r.text and os.path.isdir(os.path.join(wurzel, "asrc")), r.text  # contains a registered source
    assert c.delete(u + "/quellen/" + qs["id"]).status_code == 200
    os.makedirs(os.path.join(wurzel, "watched"))
    import konfig
    konfig.ueberwacht_eintragen({"ordner": os.path.join(wurzel, "watched"), "sammlung": "w1", "sichtbarkeit": "privat", "zugehoerigkeit": "offen"})
    r = ren(name="watched", neu="watched2")
    assert r.status_code == 409 and "watched" in r.text and os.path.isdir(os.path.join(wurzel, "watched")), r.text
    assert ren(name="asrc", neu="asrc2").status_code == 200  # source removed -> allowed again
    # mkdir conflict leaves a 'fehler' entry
    n = log_text().count("new folder refused")
    assert neu(name="fresh").status_code == 409 and log_text().count("new folder refused") == n + 1


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
