#!/usr/bin/env python3
"""Copy / move / sync onto itself must never delete anything (tab path /aktion, no MCP needed).
Cases: same file, same folder, path variants ('./sub/', 'sub//'), target folder inside the moved folder, sync of a folder onto
itself, a symlinked target folder that resolves to the source. Every refusal: HTTP 409, files unchanged, a 'fehler' line in the log."""
import importlib.util
import os
import shutil
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def stand(wurzel):
    out = {}
    for d, _, fs in os.walk(wurzel):
        for f in fs:
            p = os.path.join(d, f)
            with open(p, "rb") as h:
                out[os.path.relpath(p, wurzel)] = h.read()
    return out


def main():
    tmp = tempfile.mkdtemp(prefix="trove-selbst-")
    try:
        os.environ["TROVE_DATEN"] = os.path.join(tmp, "data")
        os.environ["HOME"] = os.environ["USERPROFILE"] = os.path.join(tmp, "home")
        os.environ.pop("TROVE_KONFIG", None)
        os.makedirs(os.environ["HOME"])
        inbox = os.path.join(tmp, "inbox")
        for d in ("sub", "vdir/inner"):
            os.makedirs(os.path.join(inbox, d))
        for rel, inhalt in (("victim.txt", "precious"), ("sub/v2.txt", "V2"), ("vdir/a.txt", "A"), ("vdir/inner/b.txt", "B")):
            with open(os.path.join(inbox, rel), "w", encoding="utf-8") as f:
                f.write(inhalt)
        os.symlink(os.path.join(inbox, "sub"), os.path.join(inbox, "zu_sub"))  # a folder link that resolves to sub
        os.symlink(".", os.path.join(inbox, "self"))  # link to an ancestor inside the same source (B2)
        os.symlink("vdir", os.path.join(inbox, "dlink"))  # link to a sibling folder
        os.symlink(os.path.join("..", "sub"), os.path.join(inbox, "vdir", "sib"))  # link inside a folder to a sibling folder
        os.symlink("..", os.path.join(inbox, "vdir", "inner", "loop"))  # nested loop back up
        os.makedirs(os.path.join(tmp, "ziel"))
        spec = importlib.util.spec_from_file_location("trove_selbst_api", os.path.join(REPO, "plugin", "dashboard", "plugin_api.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        import belegprotokoll as bp
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(mod.router)
        c = TestClient(app, raise_server_exceptions=False)
        q = c.post("/quellen", json={"pfad": inbox, "nur_lesen": False}).json()
        z = c.post("/quellen", json={"pfad": os.path.join(tmp, "ziel"), "nur_lesen": False}).json()
        vorher = stand(inbox)
        faelle = [
            ("move file onto itself", "verschieben", "", "", ["victim.txt"]),
            ("copy file onto itself", "kopieren", "", "", ["victim.txt"]),
            ("move folder onto itself", "verschieben", "", "", ["vdir"]),
            ("move file sub -> ./sub/", "verschieben", "sub", "./sub/", ["v2.txt"]),
            ("move file sub -> sub//", "verschieben", "sub", "sub//", ["v2.txt"]),
            ("move file sub -> link to sub", "verschieben", "sub", "zu_sub", ["v2.txt"]),
            ("move folder into its own subfolder", "verschieben", "", "vdir/inner", ["vdir"]),
            ("copy folder into its own subfolder", "kopieren", "", "vdir", ["vdir"]),
            ("sync folder onto itself", "sync", "sub", "./sub", []),
            ("copy link self (-> .) into vdir", "kopieren", "", "vdir", ["self"]),
            ("move link self (-> .) into vdir", "verschieben", "", "vdir", ["self"]),
            ("move link dlink (-> vdir) into vdir", "verschieben", "", "vdir", ["dlink"]),
            ("move link dlink (-> vdir) to another source", "verschieben", "", ("ziel", ""), ["dlink"]),
            ("copy sibling link vdir/sib to another source", "kopieren", "vdir", ("ziel", ""), ["sib"]),
            ("copy nested loop link to another source", "kopieren", "vdir/inner", ("ziel", ""), ["loop"]),
            ("sync from a path through a link", "sync", "dlink", ("ziel", ""), []),
            ("copy from a path through a link", "kopieren", "self/sub", ("ziel", ""), ["v2.txt"]),
            ("copy into a path through a link", "kopieren", "", "self/sub", ["victim.txt"]),
        ]
        fehler = []
        for name, art, von, nach, namen in faelle:
            n_vorher = len(list(bp.eintraege(bp.pfad_fuer(mod.MANDANT))))
            ziel_q, nach = (z["id"], nach[1]) if isinstance(nach, tuple) else (q["id"], nach)
            r = c.post("/aktion", json={"art": art, "von": {"quelle": q["id"], "pfad": von}, "nach": {"quelle": ziel_q, "pfad": nach}, "namen": namen})
            neu = list(bp.eintraege(bp.pfad_fuer(mod.MANDANT)))[n_vorher:]
            ok = r.status_code in (400, 409) and stand(inbox) == vorher and not os.listdir(os.path.join(tmp, "ziel")) and any(e["ereignis"] == "fehler" for e in neu)
            print(("ok   " if ok else "FAIL ") + name + " -> %s %s" % (r.status_code, r.json().get("detail", "")[:90]))
            if not ok:
                fehler.append(name)
        # the per-file guard alone (in case the up-front check is bypassed): never "already there" for the file itself
        with bp.Protokoll(bp.pfad_fuer(mod.MANDANT)) as prot:
            p = os.path.join(inbox, "victim.txt")
            st = mod._kopiere_datei(prot, mod.MANDANT, "t", p, os.path.join(inbox, ".", "victim.txt"))
        ok = st == "fehler" and stand(inbox) == vorher
        print(("ok   " if ok else "FAIL ") + "per-file guard -> " + st)
        if not ok:
            fehler.append("per-file guard")
        # a folder that contains links (to a sibling, and a loop back up) is copied without following them: links skipped and logged
        r = c.post("/aktion", json={"art": "kopieren", "von": {"quelle": q["id"], "pfad": ""}, "nach": {"quelle": z["id"], "pfad": ""}, "namen": ["vdir"]})
        kopiert = sorted(os.path.relpath(os.path.join(d, f), os.path.join(tmp, "ziel")) for d, _, fs in os.walk(os.path.join(tmp, "ziel")) for f in fs)
        log = list(bp.eintraege(bp.pfad_fuer(mod.MANDANT)))
        ok = (r.status_code == 200 and kopiert == [os.path.join("vdir", "a.txt"), os.path.join("vdir", "inner", "b.txt")]
              and r.json()["ergebnis"][0].get("links_skipped") == 2 and sum(1 for e in log if "link skipped" in (e.get("meldung") or "")) == 2)
        print(("ok   " if ok else "FAIL ") + "folder with links copied without following them -> %s %s" % (r.status_code, kopiert))
        if not ok:
            fehler.append("folder with links")
        shutil.rmtree(os.path.join(tmp, "ziel", "vdir"))
        # a normal move still works
        r = c.post("/aktion", json={"art": "verschieben", "von": {"quelle": q["id"], "pfad": ""}, "nach": {"quelle": z["id"], "pfad": ""}, "namen": ["victim.txt"]})
        ok = r.status_code == 200 and os.path.exists(os.path.join(tmp, "ziel", "victim.txt")) and not os.path.exists(os.path.join(inbox, "victim.txt"))
        print(("ok   " if ok else "FAIL ") + "normal move still works")
        if not ok:
            fehler.append("normal move")
        assert bp.pruefen(bp.pfad_fuer(mod.MANDANT)).get("ok", True) is not False
        if fehler:
            sys.exit("FAILED: " + ", ".join(fehler))
        print("self-target ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
