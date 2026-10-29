#!/usr/bin/env python3
"""ist_verknuepfung(): one link check for symlinks AND Windows junctions, used by copy/move/sync, rename, new folder, register
and MCP request validation. Junctions are simulated on Linux by monkeypatching os.path.isjunction for a real folder, and the
Windows reparse-point path by faking os.name/os.lstat."""
import asyncio
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import warnings

warnings.filterwarnings("ignore")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fehler = []


def pruef(name, ok, info=""):
    print(("ok   " if ok else "FAIL ") + name + (" " + str(info)[:200] if not ok else ""))
    if not ok:
        fehler.append(name)


def schreiben(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def main():
    tmp = tempfile.mkdtemp(prefix="trove-junc-")
    echt_isjunction = getattr(os.path, "isjunction", None)
    try:
        os.environ["TROVE_DATEN"] = os.path.join(tmp, "data")
        os.environ["HOME"] = os.environ["USERPROFILE"] = os.path.join(tmp, "home")
        os.environ.pop("TROVE_KONFIG", None)
        os.makedirs(os.environ["HOME"])
        von, nach = os.path.join(tmp, "von"), os.path.join(tmp, "nach")
        schreiben(os.path.join(von, "fremd", "geheim.txt"), "behind the junction")  # what the junction "points to"
        schreiben(os.path.join(von, "baum", "a.txt"), "A")
        schreiben(os.path.join(von, "baum", "junc", "j.txt"), "J")  # a real folder that we declare a junction
        os.makedirs(os.path.join(von, "baum", "junc", "leer"))
        schreiben(os.path.join(von, "solo_junc", "s.txt"), "S")
        os.makedirs(nach)
        juncs = {os.path.normpath(os.path.join(von, "baum", "junc")), os.path.normpath(os.path.join(von, "solo_junc"))}
        os.path.isjunction = lambda p: os.path.normpath(str(p)) in juncs

        spec = importlib.util.spec_from_file_location("trove_junc_api", os.path.join(REPO, "plugin", "dashboard", "plugin_api.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        iv = mod.ist_verknuepfung

        # 1) the helper itself
        pruef("plain folder is no link", not iv(os.path.join(von, "baum")))
        pruef("plain file is no link", not iv(os.path.join(von, "baum", "a.txt")))
        os.symlink(os.path.join(von, "fremd"), os.path.join(tmp, "sym"))
        pruef("symlink is a link", iv(os.path.join(tmp, "sym")))
        pruef("junction (isjunction) is a link", iv(os.path.join(von, "solo_junc")))
        pruef("missing path is no link (no exception)", not iv(os.path.join(tmp, "gibtsnicht")))
        # Windows without isjunction: reparse point with symlink/junction tag -> link; OneDrive placeholder tag -> not a link
        del os.path.isjunction
        echt_name, echt_lstat = os.name, os.lstat
        fake = {"J": types.SimpleNamespace(st_mode=0o040755, st_file_attributes=0x400, st_reparse_tag=0xA0000003),
                "S": types.SimpleNamespace(st_mode=0o040755, st_file_attributes=0x400, st_reparse_tag=0xA000000C),
                "C": types.SimpleNamespace(st_mode=0o040755, st_file_attributes=0x400, st_reparse_tag=0x9000001A),
                "N": types.SimpleNamespace(st_mode=0o040755, st_file_attributes=0x10, st_reparse_tag=0)}
        try:
            os.name = "nt"
            os.lstat = lambda p, *a, **k: fake[os.path.basename(p)] if os.path.basename(p) in fake else echt_lstat(p, *a, **k)
            res = {k: iv(os.path.join(tmp, k)) for k in fake}
        finally:
            os.name, os.lstat = echt_name, echt_lstat
        pruef("windows reparse: junction tag -> link", res["J"])
        pruef("windows reparse: symlink tag -> link", res["S"])
        pruef("windows reparse: cloud placeholder -> no link", not res["C"])
        pruef("windows: plain folder -> no link", not res["N"])
        os.path.isjunction = lambda p: os.path.normpath(str(p)) in juncs

        # 2) every place that must not follow a link
        import belegprotokoll as bp
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(mod.router)
        c = TestClient(app, raise_server_exceptions=False)
        qv = c.post("/quellen", json={"pfad": von, "nur_lesen": False}).json()
        qn = c.post("/quellen", json={"pfad": nach, "nur_lesen": False}).json()

        def aktion(art, namen, pv="", pn=""):
            return c.post("/aktion", json={"art": art, "von": {"quelle": qv["id"], "pfad": pv}, "nach": {"quelle": qn["id"], "pfad": pn}, "namen": namen})
        r = aktion("kopieren", ["solo_junc"])
        pruef("copy of a junction entry refused", r.status_code == 409 and not os.path.exists(os.path.join(nach, "solo_junc")), r.text)
        r = aktion("kopieren", ["s.txt"], pv="solo_junc")
        pruef("copy from a path through a junction refused", r.status_code == 409 and not os.path.exists(os.path.join(nach, "s.txt")), r.text)
        r = aktion("verschieben", ["baum"])
        pruef("move of a tree skips the junction (not followed, not moved)", r.status_code == 200 and os.path.isfile(os.path.join(nach, "baum", "a.txt"))
              and not os.path.exists(os.path.join(nach, "baum", "junc")) and os.path.isfile(os.path.join(von, "baum", "junc", "j.txt"))
              and os.path.isdir(os.path.join(von, "baum", "junc", "leer")) and r.json()["ergebnis"][0].get("links_skipped") == 1, r.text)
        r = c.post("/umbenennen", json={"quelle": qv["id"], "pfad": "solo_junc", "name": "s.txt", "neu": "t.txt"})
        pruef("rename through a junction refused", r.status_code == 409 and os.path.isfile(os.path.join(von, "solo_junc", "s.txt")), r.text)
        r = c.post("/ordner-neu", json={"quelle": qv["id"], "pfad": "solo_junc", "name": "neu"})
        pruef("new folder through a junction refused", r.status_code == 409 and not os.path.exists(os.path.join(von, "solo_junc", "neu")), r.text)
        r = c.post("/register", json={"quelle": qv["id"], "pfad": ""})
        con = mod._register_con(mod.MANDANT)
        pfade = [row[0] for row in con.execute("select pfad from datei")]
        pruef("register does not descend into a junction", r.status_code == 200 and not any("solo_junc" in p or os.sep + "junc" + os.sep in p for p in pfade), (r.text, pfade))
        pruef("receipt-log chain ok", bp.pruefen(bp.pfad_fuer(mod.MANDANT)).get("kette") == "ok")

        # 3) MCP request validation (needs the mcp package)
        try:
            import mcp  # noqa: F401
        except ImportError:
            print("skip MCP part (package mcp not installed)")
        else:
            sys.path.insert(0, os.path.join(REPO, "plugin", "dashboard"))
            sys.modules["plugin_api"] = mod
            import mcp_server
            srv = mcp_server.bauen(client="junction-test")

            def ruf(name, args):
                try:
                    asyncio.run(srv.call_tool(name, args))
                    return "queued"
                except Exception as ex:
                    return "refused: %s" % ex
            for name, args in (("request_copy (entry)", {"from_source": qv["id"], "to_source": qn["id"], "names": ["solo_junc"]}),
                               ("request_copy (path)", {"from_source": qv["id"], "to_source": qn["id"], "from_path": "solo_junc", "names": ["s.txt"]}),
                               ("request_rename", {"source": qv["id"], "path": "solo_junc", "name": "s.txt", "new_name": "t.txt"}),
                               ("request_new_folder", {"source": qv["id"], "path": "solo_junc", "name": "neu"})):
                erg = ruf(name.split(" ")[0], args)
                pruef("MCP %s via junction refused" % name, erg.startswith("refused") and "link" in erg, erg)
        if fehler:
            sys.exit("FAILED: " + ", ".join(fehler))
        print("links and junctions ok")
    finally:
        if echt_isjunction is not None:
            os.path.isjunction = echt_isjunction
        elif hasattr(os.path, "isjunction"):
            del os.path.isjunction
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
