#!/usr/bin/env python3
"""Trove's own places get a warning, not a lock. A source that is the data root, lies inside it or contains it
(also through a symlink) carries the field warnung in GET /quellen, in the answers of POST /quellen and /schreibbar and in MCP
list_sources; marking it writable works (200), an older quellen.json that lists it as writable stays writable, and file actions
(copy, new folder; over MCP a copy request) run there. An ordinary folder has no warning.
The same detection holds for trove.json outside the data root (~/Trove/trove.json, data on a NAS, also for a second process with
other variables), for Trove's install and Hermes' folder (HERMES_HOME), for a source that is another path (bind mount) to the data
root or a folder above it, and when TROVE_DATEN or TROVE_KONFIG changes while Trove runs."""
import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lesen(p):
    with open(p, "rb") as f:
        return f.read()


async def mcp_teil(env, ids, inbox_id, unten):
    from mcp import Client, StdioServerParameters
    from mcp.types import Implementation
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(REPO, "plugin", "dashboard", "mcp_server.py")], env=env)
    async with Client(params, client_info=Implementation(name="test-datenwurzel", version="1")) as c:
        namen = [t.name for t in (await c.list_tools()).tools]
        assert not [n for n in namen if "writ" in n or ("source" in n and n != "list_sources")], namen  # no MCP tool changes folder rights

        async def ruf(werkzeug, **args):
            r = await c.call_tool(werkzeug, args)
            return "".join(getattr(x, "text", "") for x in r.content), bool(getattr(r, "is_error", None) or getattr(r, "isError", None))
        text, err = await ruf("list_sources")
        quellen = {q["id"]: q for q in json.loads(text)}
        assert not err and {i: q["writable"] for i, q in quellen.items()} == {**{i: True for i in ids.values()}, inbox_id: True}, text
        assert all("Hermes" in quellen[i]["warnung"] for i in ids.values()) and "warnung" not in quellen[inbox_id], text
        # a copy request into the folder inside the data root runs (freigabe_pflicht off: at once)
        text, err = await ruf("request_copy", from_source=inbox_id, to_source=ids["unten"], names=["mcp.txt"])
        assert not err, text
        ende = time.time() + 30
        while not os.path.exists(os.path.join(unten, "mcp.txt")) and time.time() < ende:
            await asyncio.sleep(0.1)
        assert lesen(os.path.join(unten, "mcp.txt")) == b"M", "the MCP copy did not run"


def _api():
    spec = importlib.util.spec_from_file_location("trove_datenwurzel_api", os.path.join(REPO, "plugin", "dashboard", "plugin_api.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(mod.router)
    return mod, TestClient(app, raise_server_exceptions=False)


async def mcp_quellen(env):
    from mcp import Client, StdioServerParameters
    from mcp.types import Implementation
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(REPO, "plugin", "dashboard", "mcp_server.py")], env=env)
    async with Client(params, client_info=Implementation(name="test-datenwurzel", version="1")) as c:
        r = await c.call_tool("list_sources", {})
        return "".join(getattr(x, "text", "") for x in r.content)


def layout(tmp):
    """Child process without TROVE_DATEN: ~/Trove/trove.json names the data root on a NAS. ~/Trove holds the settings file, so
    it gets the warning too, in the tab and over MCP; marking it writable and copying into it work."""
    home, nas, inbox = os.path.join(tmp, "home"), os.path.join(tmp, "nas", "Trove"), os.path.join(tmp, "inbox")
    ht = os.path.join(home, "Trove")
    for d in (ht, nas, inbox):
        os.makedirs(d)
    tj = os.path.join(ht, "trove.json")
    with open(tj, "w", encoding="utf-8") as f:
        json.dump({"datenwurzel": nas}, f)
    with open(os.path.join(inbox, "trove.json"), "w", encoding="utf-8") as f:
        json.dump({"datenwurzel": inbox, "python": "/bin/false"}, f)
    mod, c = _api()
    import belegprotokoll as bp
    import konfig
    assert (konfig.datenwurzel(), bp.WURZEL, konfig.konfig_pfad(), konfig.freigabe_pflicht()) == (nas, konfig._START, tj, False)
    r = c.post("/quellen", json={"pfad": ht, "name": "home-trove"})
    assert r.status_code == 200 and r.json()["nur_lesen"] is True and "trove.json" in r.json()["warnung"], (r.status_code, r.text)
    hid = r.json()["id"]
    iid = c.post("/quellen", json={"pfad": inbox, "nur_lesen": False}).json()["id"]
    assert "warnung" not in c.get("/quellen").json()["quellen"][-1]  # the ordinary inbox (its own trove.json counts nowhere)
    assert not os.path.exists(os.path.join(inbox, "x"))
    r = c.post("/quellen/%s/schreibbar" % hid, json={"nur_lesen": False})
    assert r.status_code == 200 and r.json()["nur_lesen"] is False and r.json()["warnung"], (r.status_code, r.text)
    with open(os.path.join(inbox, "neu.txt"), "w", encoding="utf-8") as f:
        f.write("N")
    r = c.post("/aktion", json={"art": "kopieren", "von": {"quelle": iid, "pfad": ""}, "nach": {"quelle": hid, "pfad": ""}, "namen": ["neu.txt"]})
    assert r.status_code == 200 and lesen(os.path.join(ht, "neu.txt")) == b"N", (r.status_code, r.text)
    assert lesen(tj) == json.dumps({"datenwurzel": nas}).encode()  # nothing is overwritten, trove.json stays
    # TROVE_KONFIG changes while Trove runs (HOME too, else ~/Trove/trove.json alone would hold): the trove.json of the start stays protected
    alt_env = {n: os.environ.get(n) for n in ("TROVE_KONFIG", "HOME")}
    os.environ.update(TROVE_KONFIG=os.path.join(tmp, "anders.json"), HOME=os.path.join(tmp, "anderswo"))
    try:
        assert c.get("/quellen").json()["quellen"][0]["warnung"]
    finally:
        for n, v in alt_env.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
    # a second process with TROVE_DATEN=<nas> and no TROVE_KONFIG reads <nas>/trove.json, yet ~/Trove/trove.json still gets the warning
    env2 = {k: v for k, v in os.environ.items() if k != "TROVE_KONFIG"}
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "zweit", ht], capture_output=True, text=True, env=dict(env2, TROVE_DATEN=nas), timeout=120)
    assert r.returncode == 0 and "zweit ok" in r.stdout, (r.stdout[-2000:], r.stderr[-3000:])
    try:
        import mcp  # noqa: F401
    except ImportError:
        print("MCP part of the layout test skipped: the 'mcp' package is not installed")
    else:  # the server finds ~/Trove/trove.json itself, as after a real start
        env = {k: v for k, v in os.environ.items() if k not in ("TROVE_DATEN", "TROVE_KONFIG")}
        text = asyncio.run(mcp_quellen(dict(env, PYTHONPATH="")))
        assert "trove.json" in {q["id"]: q for q in json.loads(text)}[hid]["warnung"], text
    print("layout ok")


def zweit(ht):
    """Second process of the layout (as an MCP client may start one): TROVE_DATEN=<nas>, no TROVE_KONFIG. ~/Trove still gets the warning."""
    mod, c = _api()
    import konfig
    assert konfig.konfig_pfad() == os.path.join(konfig.datenwurzel(), "trove.json"), konfig.konfig_pfad()
    r = c.post("/quellen", json={"pfad": ht, "nur_lesen": False})
    assert r.status_code == 200 and r.json()["warnung"], (r.status_code, r.text)
    print("zweit ok")


def main():
    tmp = tempfile.mkdtemp(prefix="trove-datenwurzel-")
    try:
        env = {k: v for k, v in os.environ.items() if k not in ("TROVE_DATEN", "TROVE_KONFIG", "TROVE_FREIGABE_PFLICHT", "TROVE_MCP_CLIENT",
                                                                "TROVE_ARBEITSORDNER", "TROVE_PYTHON")}
        lt = os.path.join(tmp, "layout")
        env.update(HOME=os.path.join(lt, "home"), USERPROFILE=os.path.join(lt, "home"), PYTHONPATH="")
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "layout", lt], capture_output=True, text=True, env=env, timeout=120)
        assert r.returncode == 0 and "layout ok" in r.stdout, (r.stdout[-2000:], r.stderr[-3000:])
        print(r.stdout.strip())
        oben = os.path.join(tmp, "top")
        daten = os.path.join(oben, "Trove")
        unten = os.path.join(daten, "unter")
        link = os.path.join(tmp, "link-zur-datenwurzel")
        inbox = os.path.join(tmp, "inbox")
        for d in (unten, inbox, os.path.join(tmp, "home")):
            os.makedirs(d)
        os.symlink(daten, link)
        os.symlink(daten, os.path.join(inbox, "zu_daten"))
        with open(os.path.join(inbox, "x.txt"), "w", encoding="utf-8") as f:
            f.write("X")
        os.environ["TROVE_DATEN"] = daten
        os.environ["HOME"] = os.environ["USERPROFILE"] = os.path.join(tmp, "home")
        for n in ("TROVE_KONFIG", "TROVE_FREIGABE_PFLICHT", "TROVE_MCP_CLIENT", "TROVE_ARBEITSORDNER"):
            os.environ.pop(n, None)
        hermes = os.path.join(tmp, "hermes")  # never the real ~/.hermes; set before the import
        os.makedirs(os.path.join(hermes, "desktop-plugins", "reraise-trove"))
        os.environ["HERMES_HOME"] = hermes
        mod, c = _api()
        import belegprotokoll as bp
        import konfig
        import mcp_queue as mq
        M = mod.MANDANT
        assert bp.WURZEL == konfig._START == daten  # the files stay under the data root of the import

        # 1. data root, folder above, folder inside, symlink to it: a warning, no lock. Added writable or read-only, switched both ways: 200
        warn = lambda r: r.json().get("warnung") or ""  # noqa: E731
        ids = {}
        for name, pfad in (("daten", daten), ("oben", oben), ("unten", unten), ("link", link)):
            r = c.post("/quellen", json={"pfad": pfad, "name": name})
            assert r.status_code == 200 and r.json()["nur_lesen"] is True and "data root" in warn(r), (name, r.text)
            ids[name] = r.json()["id"]
            r = c.post("/quellen/%s/schreibbar" % ids[name], json={"nur_lesen": False})
            assert r.status_code == 200 and r.json()["nur_lesen"] is False and "agents too" in warn(r), (name, r.status_code, r.text)
            assert c.post("/quellen/%s/schreibbar" % ids[name], json={"nur_lesen": True}).status_code == 200
            r = c.post("/quellen", json={"pfad": pfad, "name": name, "nur_lesen": False})
            assert r.status_code == 200 and r.json()["nur_lesen"] is False and warn(r), (name, r.status_code, r.text)
        r = c.post("/quellen", json={"pfad": inbox, "nur_lesen": False})  # an ordinary folder: no warning
        assert r.status_code == 200 and r.json()["nur_lesen"] is False and "warnung" not in r.json(), r.text
        inbox_id = r.json()["id"]
        # Trove's install and Hermes' folder: code, settings or a trove.json put there would run or win at the next start
        for pfad in (os.path.join(REPO, "plugin", "dashboard"), os.path.join(REPO, "skills"), os.path.join(REPO, ".claude-plugin"), os.path.join(REPO, "plugin", "desktop"),
                     os.path.join(hermes, "desktop-plugins", "reraise-trove"), hermes):
            r = c.post("/quellen", json={"pfad": pfad, "nur_lesen": False})
            assert r.status_code == 200 and r.json()["nur_lesen"] is False and "install" in warn(r) and "Hermes" in warn(r), (pfad, r.status_code, r.text)
            c.delete("/quellen/" + r.json()["id"])
        quellen = {q["id"]: q for q in c.get("/quellen").json()["quellen"]}
        assert all(quellen[i]["warnung"] and not quellen[i]["nur_lesen"] for i in ids.values()) and "warnung" not in quellen[inbox_id], quellen
        qp = mod._quellen_pfad(M)
        assert '"warnung"' not in open(qp, encoding="utf-8").read()  # computed on every read, never stored
        # TROVE_DATEN changes while Trove runs: the files are still under the data root of the import, which keeps its warning
        nachbar = os.path.join(tmp, "nachbar")
        os.makedirs(nachbar)
        os.environ["TROVE_DATEN"] = nachbar
        try:
            r = c.post("/quellen", json={"pfad": daten, "nur_lesen": False})
            assert r.status_code == 200 and "data root" in warn(r), (r.status_code, r.text)
        finally:
            os.environ["TROVE_DATEN"] = daten

        # 2. an older quellen.json lists them as writable: they stay writable (not silently read-only) and file actions run
        for q in c.get("/quellen").json()["quellen"]:
            c.post("/quellen/%s/schreibbar" % q["id"], json={})
        with open(qp, encoding="utf-8") as f:
            alt = json.load(f)
        with open(qp, "w", encoding="utf-8") as f:
            json.dump([{**q, "nur_lesen": False} for q in alt], f)
        assert {q["id"]: q["nur_lesen"] for q in c.get("/quellen").json()["quellen"]} == {**{i: False for i in ids.values()}, inbox_id: False}
        prot = bp.pfad_fuer(M)
        r = c.post("/aktion", json={"art": "kopieren", "von": {"quelle": inbox_id, "pfad": ""}, "nach": {"quelle": ids["unten"], "pfad": ""}, "namen": ["x.txt"]})
        assert r.status_code == 200 and lesen(os.path.join(unten, "x.txt")) == b"X", (r.status_code, r.text)
        r = c.post("/ordner-neu", json={"quelle": ids["daten"], "pfad": "", "name": "neu"})
        assert r.status_code == 200 and os.path.isdir(os.path.join(daten, "neu")), (r.status_code, r.text)
        # a symlink inside a writable folder that points to the data root: Trove still does not write through links
        r = c.post("/aktion", json={"art": "kopieren", "von": {"quelle": inbox_id, "pfad": ""}, "nach": {"quelle": inbox_id, "pfad": "zu_daten"}, "namen": ["x.txt"]})
        assert r.status_code in (400, 409), r.text
        r = c.post("/ordner-neu", json={"quelle": inbox_id, "pfad": "zu_daten", "name": "neu2"})
        assert r.status_code in (400, 409) and not os.path.exists(os.path.join(daten, "neu2")), r.text

        # 3. over MCP (switch off: a request starts at once): list_sources shows the warning, a copy into the data root runs
        try:
            import mcp  # noqa: F401
        except ImportError:
            print("MCP part skipped: the 'mcp' package is not installed")
        else:
            with open(os.path.join(inbox, "mcp.txt"), "w", encoding="utf-8") as f:
                f.write("M")
            asyncio.run(mcp_teil(dict(os.environ, PYTHONPATH=""), ids, inbox_id, unten))
        assert bp.pruefen(prot).get("ok", True) is not False

        # 4. another path to the same folder (bind mount; on macOS another spelling): compared by identity, not by path text
        bind, bind_oben = os.path.join(tmp, "bind"), os.path.join(tmp, "bind_oben")
        os.makedirs(bind)
        os.makedirs(bind_oben)
        r = subprocess.run(["mount", "--bind", daten, bind], capture_output=True, text=True)
        if not r.returncode:
            r2 = subprocess.run(["mount", "--bind", oben, bind_oben], capture_output=True, text=True)
            if r2.returncode:
                subprocess.run(["umount", bind], check=True)
                r = r2
        if r.returncode:
            print("bind-mount part skipped: mount --bind is not allowed here (%s)" % (r.stderr.strip() or r.returncode))
        else:
            try:
                assert os.path.samefile(bind, daten) and not mod._unter_real(bind, daten)  # a real alias: realpath does not see it
                # the alias itself, a folder inside it (identity of a protected place among its ancestors) and an alias of the folder
                # above (identity of the folder among the protected place's ancestors)
                for pfad in (bind, os.path.join(bind, M), bind_oben):
                    r = c.post("/quellen", json={"pfad": pfad, "nur_lesen": False})
                    assert r.status_code == 200 and "data root" in warn(r), (pfad, r.status_code, r.text)
            finally:
                subprocess.run(["umount", bind_oben], check=True)
                subprocess.run(["umount", bind], check=True)
        print("datenwurzel ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["layout"]:
        layout(sys.argv[2])
    elif sys.argv[1:2] == ["zweit"]:
        zweit(sys.argv[2])
    else:
        main()
