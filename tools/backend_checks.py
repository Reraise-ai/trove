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
