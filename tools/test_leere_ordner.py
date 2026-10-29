#!/usr/bin/env python3
"""Copy / move / sync keep empty folders: an empty folder (or an empty subfolder inside a moved tree) is created at the target
before the source is removed, with a receipt-log line. A name clash at the target keeps the source."""
import importlib.util
import os
import shutil
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def baum(wurzel):
    out = set()
    for d, dirs, fs in os.walk(wurzel):
        for x in dirs:
            out.add(os.path.relpath(os.path.join(d, x), wurzel) + "/")
        for x in fs:
            out.add(os.path.relpath(os.path.join(d, x), wurzel))
    return out


def main():
    tmp = tempfile.mkdtemp(prefix="trove-leer-")
    try:
        os.environ["TROVE_DATEN"] = os.path.join(tmp, "data")
        os.environ["HOME"] = os.environ["USERPROFILE"] = os.path.join(tmp, "home")
        os.environ.pop("TROVE_KONFIG", None)
        os.makedirs(os.environ["HOME"])
        von, nach = os.path.join(tmp, "von"), os.path.join(tmp, "nach")
        for d in ("leer", "leer_kopie", "baum/inner/tief_leer", "baum/leer2", "sync/s_leer", "clash"):
            os.makedirs(os.path.join(von, d))
        os.makedirs(nach)
        with open(os.path.join(von, "baum", "a.txt"), "w", encoding="utf-8") as f:
            f.write("A")
        with open(os.path.join(von, "sync", "s.txt"), "w", encoding="utf-8") as f:
            f.write("S")
        with open(os.path.join(nach, "clash"), "w", encoding="utf-8") as f:
            f.write("a file where the folder would go")
        spec = importlib.util.spec_from_file_location("trove_leer_api", os.path.join(REPO, "plugin", "dashboard", "plugin_api.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
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

        def log():
            return [e for e in bp.eintraege(bp.pfad_fuer(mod.MANDANT))]
        fehler = []

        def pruef(name, ok, info=""):
            print(("ok   " if ok else "FAIL ") + name + (" " + str(info) if not ok else ""))
            if not ok:
                fehler.append(name)
        r = aktion("verschieben", ["leer"])
        pruef("move empty folder: at target, gone at source", r.status_code == 200 and os.path.isdir(os.path.join(nach, "leer"))
              and not os.path.exists(os.path.join(von, "leer")), r.text)
        pruef("move empty folder: logged", any(e.get("meldung", "").startswith("folder created at the target: leer") for e in log()))
        r = aktion("kopieren", ["leer_kopie"])
        pruef("copy empty folder: at target and source", r.status_code == 200 and os.path.isdir(os.path.join(nach, "leer_kopie"))
              and os.path.isdir(os.path.join(von, "leer_kopie")), r.text)
        r = aktion("verschieben", ["baum"])
        pruef("move tree keeps empty subfolders", r.status_code == 200 and baum(os.path.join(nach, "baum")) == {"a.txt", "inner/", "inner/tief_leer/", "leer2/"}
              and not os.path.exists(os.path.join(von, "baum")), (r.text, baum(nach)))
        os.makedirs(os.path.join(nach, "syncziel"))
        r = aktion("sync", [], pv="sync", pn="syncziel")
        pruef("sync keeps empty subfolders", r.status_code == 200 and os.path.isdir(os.path.join(nach, "syncziel", "s_leer"))
              and os.path.isfile(os.path.join(nach, "syncziel", "s.txt")), (r.text, baum(nach)))
        r = aktion("verschieben", ["clash"])
        pruef("name clash at target: source folder kept, file untouched", os.path.isdir(os.path.join(von, "clash"))
              and os.path.isfile(os.path.join(nach, "clash")) and r.status_code == 200 and r.json()["ergebnis"][0]["konflikt"] == 1, r.text)
        pruef("receipt-log chain ok", bp.pruefen(bp.pfad_fuer(mod.MANDANT)).get("kette") == "ok")
        if fehler:
            sys.exit("FAILED: " + ", ".join(fehler))
        print("empty folders ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
