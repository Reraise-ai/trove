#!/usr/bin/env python3
"""Outgoing error texts of the desk endpoints (/aktion, /umbenennen, /ordner-neu) and of released MCP requests:
every secret value read by the integrations is masked (zugang.schwaerzen) and known paths are stripped (_ohne_pfade)."""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEHEIM = "S3CRET-FT-0123456789"


def main():
    tmp = tempfile.mkdtemp(prefix="trove-ft-")
    try:
        os.environ["TROVE_DATEN"] = os.path.join(tmp, "data")
        os.environ["HOME"] = os.environ["USERPROFILE"] = os.path.join(tmp, "home")
        os.environ.pop("TROVE_KONFIG", None)
        os.makedirs(os.environ["HOME"])
        wurzel = os.path.join(tmp, "desk")
        os.makedirs(os.path.join(wurzel, "asrc", "deep"))
        with open(os.path.join(wurzel, "a.txt"), "w", encoding="utf-8") as f:
            f.write("A")
        spec = importlib.util.spec_from_file_location("trove_ft_api", os.path.join(REPO, "plugin", "dashboard", "plugin_api.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        import zugang
        import mcp_queue as mq
        import belegprotokoll as bp
        zugang.geheimnis_quelle = {"TROVE_RERAISE_KEY": GEHEIM}.get
        assert zugang.geheimnis("TROVE_RERAISE_KEY") == GEHEIM  # read once through the Hermes mock -> known to the redaction
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(mod.router)
        c = TestClient(app, raise_server_exceptions=False)
        q = c.post("/quellen", json={"pfad": wurzel, "nur_lesen": False}).json()
        c.post("/quellen", json={"pfad": os.path.join(wurzel, "asrc", "deep")})
        texte, fehler = [], []

        def pruef(name, r, code):
            texte.append(r.text)
            ok = r.status_code == code and GEHEIM not in r.text and tmp not in r.text and "/desk/" not in r.text and "<temp>" not in r.text
            print(("ok   " if ok else "FAIL ") + name + " -> %s %s" % (r.status_code, r.text[:120]))
            if not ok:
                fehler.append(name)
        pruef("rename: unknown entry named like a secret", c.post("/umbenennen", json={"quelle": q["id"], "pfad": "", "name": GEHEIM, "neu": "x"}), 404)
        pruef("rename: folder used as a source (409, no full path)", c.post("/umbenennen", json={"quelle": q["id"], "pfad": "", "name": "asrc", "neu": "b"}), 409)
        pruef("rename: target exists, secret in the new name", c.post("/umbenennen", json={"quelle": q["id"], "pfad": "", "name": "a.txt", "neu": "asrc"}), 409)
        pruef("new folder: invalid name containing a secret", c.post("/ordner-neu", json={"quelle": q["id"], "pfad": "", "name": GEHEIM + "?"}), 400)
        pruef("action: symlink name containing a secret", c.post("/aktion", json={"art": "kopieren", "von": {"quelle": q["id"], "pfad": ""},
                                                                                  "nach": {"quelle": q["id"], "pfad": "asrc"}, "namen": [GEHEIM + "/x"]}), 400)
        # a released MCP request that fails: the stored result is masked and path-free too
        e = mq.anlegen(mod.MANDANT, "test-client", "umbenennen", {"quelle": q["id"], "pfad": "", "name": GEHEIM, "neu": "y"}, "rename", "details")
        mq.freigeben(mod.MANDANT, e["id"], mod, warten=True)
        erg = mq.holen(mod.MANDANT, e["id"])
        ok = erg["status"] == "failed" and GEHEIM not in json.dumps(erg["ergebnis"]) and "***" in erg["ergebnis"]
        print(("ok   " if ok else "FAIL ") + "MCP release result masked -> " + erg["ergebnis"][:120])
        if not ok:
            fehler.append("mcp result")
        with open(bp.pfad_fuer(mod.MANDANT), encoding="utf-8") as f:
            ok = GEHEIM not in f.read()
        print(("ok   " if ok else "FAIL ") + "receipt log without the secret")
        if not ok:
            fehler.append("receipt log")
        if fehler:
            sys.exit("FAILED: " + ", ".join(fehler))
        print("error texts ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
