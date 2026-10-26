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
