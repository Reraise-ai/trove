#!/usr/bin/env python3
"""MCP access test: starts plugin/dashboard/mcp_server.py as a real MCP stdio server (and once over HTTP on 127.0.0.1),
talks to it with the MCP SDK client, and checks the core promise: an MCP client can read, but every change is only a BLOCKED
request that runs after a human releases it through the Trove tab's backend route -- never through MCP itself.
Skipped (not failed) when the `mcp` package is not installed."""
import asyncio
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
U = "/api/plugins/reraise-trove"


def _ergebnis(r):
    text = "".join(getattr(c, "text", "") for c in r.content)
    fehler = bool(getattr(r, "is_error", None) or getattr(r, "isError", None))  # mcp 2.x / 1.x
    return (json.loads(text) if not fehler else text), fehler


async def _ruf(c, werkzeug, /, **args):
    r = await c.call_tool(werkzeug, args)
    return _ergebnis(r)


def _warte(mq, mandant, kid, s=30):
    ende = time.time() + s
    while time.time() < ende:
        e = mq.holen(mandant, kid)
        if e["status"] not in ("blocked", "running"):
            return e
        time.sleep(0.1)
    raise AssertionError("request did not finish: %s" % mq.holen(mandant, kid))


async def stdio_teil(server_py, env, http, mod, tmp):
    from mcp import Client, StdioServerParameters
    from mcp.types import Implementation
    import belegprotokoll as bp
    import mcp_queue as mq
    mandant = mod.MANDANT
    inbox, archiv = os.path.join(tmp, "inbox"), os.path.join(tmp, "archive")
    q = http.post(U + "/quellen", json={"art": "ordner", "pfad": inbox, "name": "Inbox"}).json()
    z = http.post(U + "/quellen", json={"art": "ordner", "pfad": archiv, "name": "Archive"}).json()
    params = StdioServerParameters(command=sys.executable, args=[server_py], env=env)
    async with Client(params, client_info=Implementation(name="test-client", version="1")) as c:
        namen = [t.name for t in (await c.list_tools()).tools]
        assert "list_sources" in namen and "request_copy" in namen, namen
        verboten = [n for n in namen if any(w in n.lower() for w in ("release", "approve", "freigeb", "unblock", "grant", "confirm", "execute"))]
        assert not verboten, "MCP must not offer an approval tool: %s" % verboten
        # ---- read tools
        st, err = await _ruf(c, "status")
        assert not err and st["mandant"] == mandant, st
        quellen, _ = await _ruf(c, "list_sources")
        assert {x["name"]: x["writable"] for x in quellen} == {"Inbox": False, "Archive": False}, quellen
        ls, _ = await _ruf(c, "list_folder", source=q["id"])
        assert [e["name"] for e in ls["entries"]] == ["sub", "note.txt"], ls
        pv, _ = await _ruf(c, "preview", source=q["id"], path="note.txt")
        assert pv["inhalt"] == "hello trove" and pv["kind"] == "text", pv
        _, err = await _ruf(c, "preview", source=q["id"], path="../../etc/passwd")
        assert err, "path escape must fail"
        su, err = await _ruf(c, "search", query="Source added")
        assert not err and su["protokoll"], su
        tr, err = await _ruf(c, "file_trail", file=os.path.join(inbox, "note.txt"))
        assert not err, tr
        ch, err = await _ruf(c, "check_changes", source=q["id"])
        assert not err and ch["erster_lauf"] is True, ch
        # ---- write into a read-only folder: refused, nothing queued, nothing written
        msg, err = await _ruf(c, "request_copy", from_source=q["id"], to_source=z["id"], names=["note.txt"])
        assert err and "read-only" in msg and not os.listdir(archiv) and mq.liste(mandant) == [], msg
        http.post(U + "/quellen/" + z["id"] + "/schreibbar", json={"nur_lesen": False})  # the human marks the target writable
        msg, err = await _ruf(c, "request_move", from_source=q["id"], to_source=z["id"], names=["note.txt"])
        assert err and "source folder" in msg, msg  # move needs a writable source as well
        _, err = await _ruf(c, "request_copy", from_source=q["id"], to_source=z["id"], names=["../x"])
        assert err
        # ---- copy request: blocked, nothing on disk
        vorher = sorted(os.listdir(inbox)), sorted(os.listdir(archiv))
        r, err = await _ruf(c, "request_copy", from_source=q["id"], to_source=z["id"], names=["note.txt", "sub"], reason="file the note")
        assert not err and r["status"] == "blocked" and r["task_id"].startswith("mcp-"), r
        kid = r["task_id"]
        time.sleep(0.5)
        assert (sorted(os.listdir(inbox)), sorted(os.listdir(archiv))) == vorher, "a request must not touch the disk"
        ts, _ = await _ruf(c, "task_status", task_id=kid)
        assert ts["status"] == "blocked" and ts["client"] == "test-client", ts
        # every MCP tool once more: none of them may move the request on
        for name, a in (("list_tasks", {}), ("task_status", {"task_id": kid}), ("status", {})):
            await _ruf(c, name, **a)
        assert mq.holen(mandant, kid)["status"] == "blocked" and not os.listdir(archiv)
        # ---- the Trove tab shows it with the client's name
        tab = http.get(U + "/auftraege").json()["auftraege"]
        karte = [a for a in tab if a["id"] == kid]
        assert karte and karte[0]["status"] == "blocked" and "MCP test-client" in karte[0]["titel"] and "file the note" in karte[0]["zusammenfassung"], tab
        # ---- human release through the backend route -> executed, verified, logged
        rr = http.post(U + "/auftrag/%s/freigeben" % kid, json={})
        assert rr.status_code == 200, rr.text
        e = _warte(mq, mandant, kid)
        assert e["status"] == "done", e
        with open(os.path.join(archiv, "note.txt"), encoding="utf-8") as f:
            assert f.read() == "hello trove"
        assert os.path.isfile(os.path.join(archiv, "sub", "deep.txt")) and os.path.isfile(os.path.join(inbox, "note.txt"))
        assert http.post(U + "/auftrag/%s/freigeben" % kid, json={}).status_code == 409  # only once
        ts, _ = await _ruf(c, "task_status", task_id=kid)
        assert ts["status"] == "done", ts
        # ---- rejected request never runs
        with open(os.path.join(inbox, "late.txt"), "w", encoding="utf-8") as f:
            f.write("late")
        r, err = await _ruf(c, "request_sync", from_source=q["id"], to_source=z["id"])
        assert not err, r
        assert http.post(U + "/auftrag/%s/archivieren" % r["task_id"], json={}).status_code == 200
        assert mq.holen(mandant, r["task_id"])["status"] == "rejected"
        assert http.post(U + "/auftrag/%s/freigeben" % r["task_id"], json={}).status_code == 409
        assert not os.path.exists(os.path.join(archiv, "late.txt"))
        # ---- permission re-checked at release: target switched back to read-only meanwhile -> fails, nothing written
        r, err = await _ruf(c, "request_copy", from_source=q["id"], to_source=z["id"], names=["late.txt"])
        assert not err, r
        http.post(U + "/quellen/" + z["id"] + "/schreibbar", json={})
        assert http.post(U + "/auftrag/%s/freigeben" % r["task_id"], json={}).status_code == 200
        e = _warte(mq, mandant, r["task_id"])
        assert e["status"] == "failed" and "read-only" in e["ergebnis"] and not os.path.exists(os.path.join(archiv, "late.txt")), e
        # ---- into knowledge: also only a request
        r, err = await _ruf(c, "request_into_knowledge", source=q["id"], collection="mcp-test")
        assert not err and r["status"] == "blocked", r
        wiki = os.path.join(os.environ["TROVE_DATEN"], "knowledge")
        assert not os.path.exists(wiki) or not any(f.endswith(".md") for _, _, fs in os.walk(wiki) for f in fs), "nothing before release"
        assert http.post(U + "/auftrag/%s/freigeben" % r["task_id"], json={}).status_code == 200
        e = _warte(mq, mandant, r["task_id"], 120)
        assert e["status"] == "done", e
        zwillinge = [f for d, _, fs in os.walk(os.path.join(wiki, "raw", "trove", "mcp-test")) for f in fs if f.endswith(".md")]
        assert any(f.endswith("_note.md") for f in zwillinge), zwillinge
        # ---- rename / new folder (only when the backend has them)
        if hasattr(mod, "umbenennen"):
            http.post(U + "/quellen/" + z["id"] + "/schreibbar", json={"nur_lesen": False})
            r, err = await _ruf(c, "request_rename", source=z["id"], name="note.txt", new_name="renamed.txt")
            assert not err and r["status"] == "blocked" and os.path.exists(os.path.join(archiv, "note.txt")), r
            assert http.post(U + "/auftrag/%s/freigeben" % r["task_id"], json={}).status_code == 200
            assert _warte(mq, mandant, r["task_id"])["status"] == "done"
            assert os.path.exists(os.path.join(archiv, "renamed.txt")) and not os.path.exists(os.path.join(archiv, "note.txt"))
            _, err = await _ruf(c, "request_rename", source=z["id"], name="renamed.txt", new_name="sub")
            assert err  # would replace an existing entry
            _, err = await _ruf(c, "request_rename", source=q["id"], name="note.txt", new_name="x.txt")
            assert err  # read-only source
        if hasattr(mod, "ordner_neu"):
            r, err = await _ruf(c, "request_new_folder", source=z["id"], name="2026")
            assert not err and not os.path.exists(os.path.join(archiv, "2026")), r
            assert http.post(U + "/auftrag/%s/freigeben" % r["task_id"], json={}).status_code == 200
            assert _warte(mq, mandant, r["task_id"])["status"] == "done" and os.path.isdir(os.path.join(archiv, "2026"))
    # ---- receipt log: every MCP step with the client's name, chain intact
    pf = bp.pfad_fuer(mandant)
    alle = list(bp.eintraege(pf))
    mcp = [x for x in alle if x.get("werkzeug") == "trove-mcp"]
    texte = " | ".join(x.get("meldung", "") for x in mcp)
    for teil in ("refused before queueing", "created (blocked", "released by a human", kid + " from test-client done", "rejected by a human", "failed", "check_changes"):
        assert teil in texte, (teil, texte)
    assert all(x.get("mcp_client") == "test-client" for x in mcp if "mcp_client" in x), mcp
    assert any(x["ereignis"] == "verifiziert" and x.get("ziel", "").endswith("note.txt") for x in alle)
    pr = bp.pruefen(pf)
    assert pr.get("ok", True) is not False, pr


async def http_teil(server_py, env):
    from mcp import Client
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    p = subprocess.Popen([sys.executable, server_py, "--http", str(port), "--client-name", "http-test"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        ende = time.time() + 30
        while time.time() < ende:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.2)
        out = subprocess.run(["ss", "-ltn"], capture_output=True, text=True).stdout if shutil.which("ss") else ""
        if out:
            zeilen = [z for z in out.splitlines() if ":%d " % port in z]
            assert zeilen and all("127.0.0.1:%d" % port in z for z in zeilen), zeilen  # localhost only
        async with Client("http://127.0.0.1:%d/mcp" % port) as c:
            st, err = await _ruf(c, "status")
            assert not err and st["mcp_client"] == "http-test", st
    finally:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


def main():
    try:
        import mcp  # noqa: F401
    except ImportError:
        print("mcp test skipped: the 'mcp' package is not installed")
        return
    tmp = tempfile.mkdtemp(prefix="trove-mcp-")
    try:
        plugin = os.path.join(tmp, "plugins", "reraise-trove")
        shutil.copytree(os.path.join(REPO, "plugin"), plugin, ignore=shutil.ignore_patterns("__pycache__"))
        os.environ["TROVE_DATEN"] = os.path.join(tmp, "data")
        os.environ["HOME"] = os.environ["USERPROFILE"] = os.path.join(tmp, "home")
        os.environ.pop("TROVE_KONFIG", None)
        os.environ.pop("TROVE_MCP_CLIENT", None)
        os.makedirs(os.environ["HOME"])
        for d in ("inbox/sub", "archive"):
            os.makedirs(os.path.join(tmp, d))
        with open(os.path.join(tmp, "inbox", "note.txt"), "w", encoding="utf-8") as f:
            f.write("hello trove")
        with open(os.path.join(tmp, "inbox", "sub", "deep.txt"), "w", encoding="utf-8") as f:
            f.write("deep")
        dash = os.path.join(plugin, "dashboard")
        spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_trove", os.path.join(dash, "plugin_api.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(mod.router, prefix=U)
        http = TestClient(app)
        env = dict(os.environ, PYTHONPATH="")
        server_py = os.path.join(dash, "mcp_server.py")
        asyncio.run(stdio_teil(server_py, env, http, mod, tmp))
        asyncio.run(http_teil(server_py, env))
        print("mcp ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
