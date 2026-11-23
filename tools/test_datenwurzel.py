#!/usr/bin/env python3
"""The Trove data root is never writable. Marking a source writable that is the data root, lies inside it or contains it (also
through a symlink) is refused with 400; adding it read-only works. An older quellen.json that already lists such a folder as
writable changes nothing: copy, move, rename and new folder are refused in the tab and over MCP, and Trove's own
mcp_auftraege.json and belegprotokoll.jsonl stay as they were (over MCP the refusal itself is logged, appended to the log).
The same holds for trove.json outside the data root (~/Trove/trove.json, data on a NAS, also for a second process with other
variables), for Trove's install and Hermes' folder (HERMES_HOME), for a source that is another path (bind mount) to the data root or a folder above it,
and when TROVE_DATEN or TROVE_KONFIG changes while Trove runs: the files stay where they were at import."""
import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lesen(p):
    with open(p, "rb") as f:
        return f.read()


async def mcp_teil(env, ids, inbox_id, M, prot, queue):
    from mcp import Client, StdioServerParameters
    from mcp.types import Implementation
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(REPO, "plugin", "dashboard", "mcp_server.py")], env=env)
    vorher_prot, vorher_queue = lesen(prot), lesen(queue)
    async with Client(params, client_info=Implementation(name="test-datenwurzel", version="1")) as c:
        namen = [t.name for t in (await c.list_tools()).tools]
        assert not [n for n in namen if "writ" in n or ("source" in n and n != "list_sources")], namen  # no MCP tool changes folder rights

        async def ruf(werkzeug, **args):
            r = await c.call_tool(werkzeug, args)
            return "".join(getattr(x, "text", "") for x in r.content), bool(getattr(r, "is_error", None) or getattr(r, "isError", None))
        text, err = await ruf("list_sources")
        assert not err and {q["id"]: q["writable"] for q in json.loads(text)} == {**{i: False for i in ids.values()}, inbox_id: True}, text
        versuche = []
        for name, qid in ids.items():
            versuche += [("request_copy", dict(from_source=inbox_id, to_source=qid, names=["x.txt"])),
                         ("request_sync", dict(from_source=inbox_id, to_source=qid)),
                         ("request_new_folder", dict(source=qid, name="neu"))]
        versuche += [("request_move", dict(from_source=ids["daten"], from_path=M, to_source=inbox_id, names=["belegprotokoll.jsonl"])),
                     ("request_move", dict(from_source=ids["oben"], to_source=inbox_id, names=["Trove"])),
                     ("request_rename", dict(source=ids["daten"], path=M, name="mcp_auftraege.json", new_name="weg.json")),
                     ("request_rename", dict(source=ids["link"], path=M, name="belegprotokoll.jsonl", new_name="weg.jsonl"))]
        for werkzeug, args in versuche:
            text, err = await ruf(werkzeug, **args)
            assert err and "read-only" in text, (werkzeug, args, text)
    assert lesen(queue) == vorher_queue, "mcp_auftraege.json changed"
    neu = lesen(prot)
    assert neu.startswith(vorher_prot), "belegprotokoll.jsonl: existing lines changed"
    zeilen = [json.loads(z) for z in neu[len(vorher_prot):].decode("utf-8").splitlines() if z.strip()]
    assert len(zeilen) == len(versuche) and all(z["ereignis"] == "notiz" and "refused before queueing" in z["meldung"] for z in zeilen), zeilen


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


async def mcp_rename(env, qid):
    from mcp import Client, StdioServerParameters
    from mcp.types import Implementation
    params = StdioServerParameters(command=sys.executable, args=[os.path.join(REPO, "plugin", "dashboard", "mcp_server.py")], env=env)
    async with Client(params, client_info=Implementation(name="test-datenwurzel", version="1")) as c:
        r = await c.call_tool("request_rename", dict(source=qid, name="trove.json", new_name="alt.json"))
        return "".join(getattr(x, "text", "") for x in r.content), bool(getattr(r, "is_error", None) or getattr(r, "isError", None))


def layout(tmp):
    """Child process without TROVE_DATEN: ~/Trove/trove.json names the data root on a NAS. ~/Trove holds the settings file, so
    it is never writable either: tab and MCP refuse to rename or overwrite trove.json."""
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
    import mcp_queue as mq
    M = mod.MANDANT
    assert (konfig.datenwurzel(), bp.WURZEL, konfig.konfig_pfad(), konfig.freigabe_pflicht()) == (nas, konfig._START, tj, False)
    r = c.post("/quellen", json={"pfad": ht, "nur_lesen": False})
    assert r.status_code == 400 and "trove.json" in r.json()["detail"], (r.status_code, r.text)
    hid = c.post("/quellen", json={"pfad": ht, "name": "home-trove"}).json()["id"]
    iid = c.post("/quellen", json={"pfad": inbox, "nur_lesen": False}).json()["id"]
    qp = mod._quellen_pfad(M)
    with open(qp, encoding="utf-8") as f:
        alt = json.load(f)
    with open(qp, "w", encoding="utf-8") as f:  # an older quellen.json with ~/Trove writable
        json.dump([{**q, "nur_lesen": False} for q in alt], f)
    with open(mq._pfad(M), "w", encoding="utf-8") as f:
        json.dump([], f)
    vorher = (lesen(tj), lesen(mq._pfad(M)), sorted(os.listdir(ht)))
    for route, body in (("/umbenennen", {"quelle": hid, "pfad": "", "name": "trove.json", "neu": "alt.json"}),
                        ("/aktion", {"art": "kopieren", "von": {"quelle": iid, "pfad": ""}, "nach": {"quelle": hid, "pfad": ""}, "namen": ["trove.json"]})):
        r = c.post(route, json=body)
        assert r.status_code == 403 and "read-only" in r.json()["detail"], (route, r.status_code, r.text)
    # TROVE_KONFIG changes while Trove runs (HOME too, else ~/Trove/trove.json alone would hold): the trove.json of the start stays protected
    alt_env = {n: os.environ.get(n) for n in ("TROVE_KONFIG", "HOME")}
    os.environ.update(TROVE_KONFIG=os.path.join(tmp, "anders.json"), HOME=os.path.join(tmp, "anderswo"))
    try:
        r = c.post("/quellen", json={"pfad": ht, "nur_lesen": False})
        assert r.status_code == 400, (r.status_code, r.text)
    finally:
        for n, v in alt_env.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
    # a second process with TROVE_DATEN=<nas> and no TROVE_KONFIG reads <nas>/trove.json, yet ~/Trove/trove.json stays protected
    env2 = {k: v for k, v in os.environ.items() if k != "TROVE_KONFIG"}
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "zweit", ht], capture_output=True, text=True, env=dict(env2, TROVE_DATEN=nas), timeout=120)
    assert r.returncode == 0 and "zweit ok" in r.stdout, (r.stdout[-2000:], r.stderr[-3000:])
    try:
        import mcp  # noqa: F401
    except ImportError:
        print("MCP part of the layout test skipped: the 'mcp' package is not installed")
    else:  # the server finds ~/Trove/trove.json itself, as after a real start
        env = {k: v for k, v in os.environ.items() if k not in ("TROVE_DATEN", "TROVE_KONFIG")}
        text, err = asyncio.run(mcp_rename(dict(env, PYTHONPATH=""), hid))
        assert err and "read-only" in text, text
    assert (lesen(tj), lesen(mq._pfad(M)), sorted(os.listdir(ht))) == vorher
    print("layout ok")


def zweit(ht):
    """Second process of the layout (as an MCP client may start one): TROVE_DATEN=<nas>, no TROVE_KONFIG. ~/Trove stays protected."""
    mod, c = _api()
    import konfig
    assert konfig.konfig_pfad() == os.path.join(konfig.datenwurzel(), "trove.json"), konfig.konfig_pfad()
    r = c.post("/quellen", json={"pfad": ht, "nur_lesen": False})
    assert r.status_code == 400, (r.status_code, r.text)
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

        # 1. marking writable: data root, folder above, folder inside, symlink to it -> 400 with the reason; read-only works
        ids = {}
        for name, pfad in (("daten", daten), ("oben", oben), ("unten", unten), ("link", link)):
            r = c.post("/quellen", json={"pfad": pfad, "nur_lesen": False})
            assert r.status_code == 400 and "data root" in r.json()["detail"], (name, r.status_code, r.text)
            r = c.post("/quellen", json={"pfad": pfad, "name": name})
            assert r.status_code == 200 and r.json()["nur_lesen"] is True, (name, r.text)
            ids[name] = r.json()["id"]
            r = c.post("/quellen/%s/schreibbar" % ids[name], json={"nur_lesen": False})
            assert r.status_code == 400 and "data root" in r.json()["detail"], (name, r.status_code, r.text)
            assert c.post("/quellen/%s/schreibbar" % ids[name], json={"nur_lesen": True}).status_code == 200
        r = c.post("/quellen", json={"pfad": inbox, "nur_lesen": False})  # an ordinary folder still becomes writable
        assert r.status_code == 200 and r.json()["nur_lesen"] is False, r.text
        inbox_id = r.json()["id"]
        # Trove's install and Hermes' folder: code, settings or a trove.json put there would run or win at the next start
        for pfad in (os.path.join(REPO, "plugin", "dashboard"), os.path.join(REPO, "skills"), os.path.join(REPO, ".claude-plugin"),
                     os.path.join(REPO, "plugin", "desktop"), os.path.join(hermes, "desktop-plugins", "reraise-trove"), hermes):
            r = c.post("/quellen", json={"pfad": pfad, "nur_lesen": False})
            assert r.status_code == 400, (pfad, r.status_code, r.text)
            assert pfad != os.path.join(REPO, "skills") or "install" in r.json()["detail"], r.text
            assert pfad != hermes or "Hermes" in r.json()["detail"], r.text
        assert c.post("/quellen/%s/schreibbar" % inbox_id, json={"nur_lesen": False}).status_code == 200
        assert all(q["nur_lesen"] for q in c.get("/quellen").json()["quellen"] if q["id"] in ids.values())
        # TROVE_DATEN changes while Trove runs: the files are still under the data root of the import, which stays protected
        nachbar = os.path.join(tmp, "nachbar")
        os.makedirs(nachbar)
        os.environ["TROVE_DATEN"] = nachbar
        try:
            r = c.post("/quellen", json={"pfad": daten, "nur_lesen": False})
            assert r.status_code == 400 and "data root" in r.json()["detail"], (r.status_code, r.text)
        finally:
            os.environ["TROVE_DATEN"] = daten

        # 2. an older quellen.json lists all four as writable: every file action that would write there is refused
        qp = mod._quellen_pfad(M)
        with open(qp, encoding="utf-8") as f:
            alt = json.load(f)
        with open(qp, "w", encoding="utf-8") as f:
            json.dump([{**q, "nur_lesen": False} for q in alt], f)
        with open(mq._pfad(M), "w", encoding="utf-8") as f:
            json.dump([], f)
        prot, queue = bp.pfad_fuer(M), mq._pfad(M)
        vorher = (lesen(prot), lesen(queue), sorted(os.listdir(daten)), sorted(os.listdir(os.path.join(daten, M))), sorted(os.listdir(inbox)))
        assert {q["id"]: q["nur_lesen"] for q in c.get("/quellen").json()["quellen"]} == {**{i: True for i in ids.values()}, inbox_id: False}
        versuche = []
        for name, qid in ids.items():
            versuche += [("/aktion", {"art": "kopieren", "von": {"quelle": inbox_id, "pfad": ""}, "nach": {"quelle": qid, "pfad": ""}, "namen": ["x.txt"]}),
                         ("/aktion", {"art": "sync", "von": {"quelle": inbox_id, "pfad": ""}, "nach": {"quelle": qid, "pfad": ""}}),
                         ("/ordner-neu", {"quelle": qid, "pfad": "", "name": "neu"})]
        versuche += [("/aktion", {"art": "verschieben", "von": {"quelle": ids["daten"], "pfad": M}, "nach": {"quelle": inbox_id, "pfad": ""},
                                  "namen": ["belegprotokoll.jsonl"]}),
                     ("/aktion", {"art": "verschieben", "von": {"quelle": ids["oben"], "pfad": ""}, "nach": {"quelle": inbox_id, "pfad": ""}, "namen": ["Trove"]}),
                     ("/umbenennen", {"quelle": ids["daten"], "pfad": M, "name": "mcp_auftraege.json", "neu": "weg.json"}),
                     ("/umbenennen", {"quelle": ids["link"], "pfad": M, "name": "belegprotokoll.jsonl", "neu": "weg.jsonl"})]
        for route, body in versuche:
            r = c.post(route, json=body)
            assert r.status_code == 403 and "read-only" in r.json()["detail"], (route, body, r.status_code, r.text)
        assert (lesen(prot), lesen(queue), sorted(os.listdir(daten)), sorted(os.listdir(os.path.join(daten, M))), sorted(os.listdir(inbox))) == vorher
        # a symlink inside a writable folder that points to the data root: Trove does not write through it
        r = c.post("/aktion", json={"art": "kopieren", "von": {"quelle": inbox_id, "pfad": ""}, "nach": {"quelle": inbox_id, "pfad": "zu_daten"}, "namen": ["x.txt"]})
        assert r.status_code in (400, 409), r.text
        r = c.post("/ordner-neu", json={"quelle": inbox_id, "pfad": "zu_daten", "name": "neu"})
        assert r.status_code in (400, 409), r.text
        assert lesen(queue) == vorher[1] and sorted(os.listdir(daten)) == vorher[2] and sorted(os.listdir(os.path.join(daten, M))) == vorher[3]

        # 3. the same over MCP (switch off: a request would start at once): refused before queueing
        try:
            import mcp  # noqa: F401
        except ImportError:
            print("MCP part skipped: the 'mcp' package is not installed")
        else:
            asyncio.run(mcp_teil(dict(os.environ, PYTHONPATH=""), ids, inbox_id, M, prot, queue))
        assert (sorted(os.listdir(daten)), sorted(os.listdir(os.path.join(daten, M))), sorted(os.listdir(inbox))) == vorher[2:]
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
                    assert r.status_code == 400 and "data root" in r.json()["detail"], (pfad, r.status_code, r.text)
                bid = c.post("/quellen", json={"pfad": bind, "name": "bind"}).json()["id"]
                with open(qp, encoding="utf-8") as f:
                    alt = json.load(f)
                with open(qp, "w", encoding="utf-8") as f:  # an older quellen.json with the alias writable
                    json.dump([{**q, "nur_lesen": False} for q in alt], f)
                vorher_q = lesen(qp)
                for route, body in (("/umbenennen", {"quelle": bid, "pfad": M, "name": "quellen.json", "neu": "weg.json"}),
                                    ("/aktion", {"art": "kopieren", "von": {"quelle": inbox_id, "pfad": ""}, "nach": {"quelle": bid, "pfad": M}, "namen": ["x.txt"]})):
                    r = c.post(route, json=body)
                    assert r.status_code == 403 and "read-only" in r.json()["detail"], (route, r.status_code, r.text)
                assert lesen(qp) == vorher_q and sorted(os.listdir(os.path.join(daten, M))) == vorher[3]
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
