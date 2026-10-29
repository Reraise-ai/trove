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
