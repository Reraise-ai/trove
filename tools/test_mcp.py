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
OK = {"confirm": True}  # what the tab sends with Release / Archive


HERMES_LOG = ""


def _stand(wurzel):
    """Every file under wurzel (outside Trove's data folder) with its content: proves that nothing on disk changed."""
    out = {}
    for d, dirs, fs in os.walk(wurzel):
        dirs[:] = [x for x in dirs if x not in ("data", "plugins", "home")]
        for f in fs:
            p = os.path.join(d, f)
            if not os.path.islink(p) and f not in ("hermes", "hermes-calls.log"):
                with open(p, "rb") as h:
                    out[os.path.relpath(p, wurzel)] = h.read()
    return out


FAKE_HERMES = """#!%s
import json, sys
with open(%r, "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
a = sys.argv[1:]
if a[:2] == ["kanban", "create"]:
    print(json.dumps({"id": "t_fake1"}))
elif a[:2] == ["kanban", "ls"]:
    print("[]")
else:
    print("{}")
"""


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
        # ---- B1 / S3 / S4: requests that act on themselves, '..' paths, links leaving the source, oversized input: refused up front
        w = http.post(U + "/quellen", json={"art": "ordner", "pfad": os.path.join(tmp, "work"), "name": "Work", "nur_lesen": False}).json()
        arbeit = os.path.join(tmp, "work")
        disk = _stand(tmp)
        n_queue = len(mq.liste(mandant))
        for name, werkzeug, args, erwartet in (
                ("move file onto itself", "request_move", dict(from_source=w["id"], to_source=w["id"], names=["w.txt"]), "same entry"),
                ("copy file onto itself", "request_copy", dict(from_source=w["id"], to_source=w["id"], names=["w.txt"]), "same entry"),
                ("move folder onto itself", "request_move", dict(from_source=w["id"], to_source=w["id"], names=["wdir"]), "same entry"),
                ("move sub -> ./sub/", "request_move", dict(from_source=w["id"], from_path="wsub", to_source=w["id"], to_path="./wsub/", names=["y.txt"]), "same entry"),
                ("move folder into itself", "request_move", dict(from_source=w["id"], to_source=w["id"], to_path="wdir", names=["wdir"]), "inside the folder"),
                ("sync folder onto itself", "request_sync", dict(from_source=w["id"], to_source=w["id"]), "inside the source"),
                ("'..' in to_path", "request_copy", dict(from_source=w["id"], to_source=z["id"], to_path="../../etc", names=["w.txt"]), "'..'"),
                ("'..' in from_path", "request_copy", dict(from_source=w["id"], from_path="wsub/..", to_source=z["id"], names=["w.txt"]), "'..'"),
                ("file link leaving the source", "request_copy", dict(from_source=w["id"], to_source=z["id"], names=["aussen"]), "leave the source"),
                ("folder link leaving the source", "request_copy", dict(from_source=w["id"], to_source=z["id"], names=["aussendir"]), "leave the source"),
                ("too many names", "request_copy", dict(from_source=w["id"], to_source=z["id"], names=["w.txt"] * 201), "at most 200"),
                ("reason too long", "request_copy", dict(from_source=w["id"], to_source=z["id"], names=["w.txt"], reason="x" * 501), "at most 500"),
                ("huge path", "request_copy", dict(from_source=w["id"], to_source=z["id"], to_path="a" * 200000, names=["w.txt"]), "at most 1024"),
                ("agent task text too long", "request_agent_task", dict(title="t", text="x" * 4001), "at most 4000"),
                ("link self (-> .) into a subfolder", "request_copy", dict(from_source=w["id"], to_source=w["id"], to_path="wdir", names=["self"]), "links"),
                ("move link dlink (-> wdir)", "request_move", dict(from_source=w["id"], to_source=z["id"], names=["dlink"]), "links"),
                ("sync from a path through a link", "request_sync", dict(from_source=w["id"], from_path="dlink", to_source=z["id"]), "goes through a link"),
                ("copy into a path through a link", "request_copy", dict(from_source=w["id"], to_source=w["id"], to_path="self/wsub", names=["w.txt"]), "goes through a link")):
            msg, err = await _ruf(c, werkzeug, **args)
            assert err and erwartet in msg, (name, msg)
        assert _stand(tmp) == disk and len(mq.liste(mandant)) == n_queue, "refused requests must leave disk and queue untouched"
        # B1 at release time too (a request queued without the up-front check, e.g. by an older version): refused, nothing deleted
        for von, nach, namen in (("", "", ["w.txt"]), ("", "", ["wdir"]), ("wsub", "./wsub/", ["y.txt"])):
            e = mq.anlegen(mandant, "test-client", "verschieben", {"von": {"quelle": w["id"], "pfad": von}, "nach": {"quelle": w["id"], "pfad": nach}, "namen": namen}, "t", "d")
            assert http.post(U + "/auftrag/%s/freigeben" % e["id"], json=OK).status_code == 200
            e = _warte(mq, mandant, e["id"])
            assert e["status"] == "failed" and "Nothing was copied, moved or deleted" in e["ergebnis"] and _stand(tmp) == disk, e
        # B2 at release time: a queued request that names a link pointing back up -> refused, no recursion, nothing written
        e = mq.anlegen(mandant, "test-client", "kopieren", {"von": {"quelle": w["id"], "pfad": ""}, "nach": {"quelle": w["id"], "pfad": "wdir"}, "namen": ["self"]}, "t", "d")
        assert http.post(U + "/auftrag/%s/freigeben" % e["id"], json=OK).status_code == 200
        e = _warte(mq, mandant, e["id"])
        assert e["status"] == "failed" and "is a link" in e["ergebnis"] and _stand(tmp) == disk, e
        # B3: links INSIDE the target are never written through (read-only source behind a link, nested link to outside)
        t1 = http.post(U + "/quellen", json={"art": "ordner", "pfad": os.path.join(tmp, "tgt"), "name": "Tgt", "nur_lesen": False}).json()
        t2 = http.post(U + "/quellen", json={"art": "ordner", "pfad": os.path.join(tmp, "tgt2"), "name": "Tgt2", "nur_lesen": False}).json()
        http.post(U + "/quellen", json={"art": "ordner", "pfad": os.path.join(tmp, "ro"), "name": "RO"})
        for name, werkzeug, args in (("target counterpart is a link to a read-only source", "request_copy", dict(from_source=w["id"], to_source=t1["id"], names=["wdir"])),
                                     ("move, target counterpart is a link", "request_move", dict(from_source=w["id"], to_source=t1["id"], names=["wdir"])),
                                     ("nested link in the target to outside", "request_move", dict(from_source=w["id"], to_source=t2["id"], names=["wdir"])),
                                     ("sync into a target with a nested link", "request_sync", dict(from_source=w["id"], from_path="wdir", to_source=t2["id"], to_path="wdir")),
                                     ("S6: into knowledge, path through a link", "request_into_knowledge", dict(source=w["id"], path="dlink")),
                                     ("S6: into knowledge, file through a link", "request_into_knowledge", dict(source=w["id"], files=["dlink/x.txt"]))):
            msg, err = await _ruf(c, werkzeug, **args)
            assert err and "link" in msg, (name, msg)
        assert _stand(tmp) == disk
        for ziel in (t1, t2):  # queued past the request check (older version): refused per file at release, nothing written, nothing deleted
            e = mq.anlegen(mandant, "test-client", "verschieben", {"von": {"quelle": w["id"], "pfad": ""}, "nach": {"quelle": ziel["id"], "pfad": ""}, "namen": ["wdir"]}, "t", "d")
            assert http.post(U + "/auftrag/%s/freigeben" % e["id"], json=OK).status_code == 200
            e = _warte(mq, mandant, e["id"])
            assert e["status"] == "failed", e
        assert os.listdir(os.path.join(tmp, "ro")) == ["keep.txt"] and os.listdir(os.path.join(tmp, "outside2")) == []
        assert os.path.isfile(os.path.join(arbeit, "wdir", "deep", "z.txt"))
        # the nested case moved x.txt (safe) and kept deep/z.txt; put x.txt back for the rest of the test
        if os.path.exists(os.path.join(tmp, "tgt2", "wdir", "x.txt")):
            shutil.move(os.path.join(tmp, "tgt2", "wdir", "x.txt"), os.path.join(arbeit, "wdir", "x.txt"))
        disk = _stand(tmp)
        # S2: release only as a JSON request with {"confirm": true} and no foreign origin
        e = mq.anlegen(mandant, "test-client", "kopieren", {"von": {"quelle": w["id"], "pfad": ""}, "nach": {"quelle": z["id"], "pfad": ""}, "namen": ["w.txt"]}, "t", "d")
        url = U + "/auftrag/%s/freigeben" % e["id"]
        for name, kw in (("no body", {}), ("JSON without confirm", {"json": {}}), ("confirm false", {"json": {"confirm": False}}),
                         ("multipart form", {"files": {"confirm": (None, "true")}}), ("empty multipart", {"content": b"", "headers": {"Content-Type": "multipart/form-data; boundary=x"}}),
                         ("urlencoded form", {"data": {"confirm": "true"}}), ("text/plain", {"content": b'{"confirm": true}', "headers": {"Content-Type": "text/plain"}}),
                         ("foreign origin", {"json": OK, "headers": {"Origin": "http://evil.example"}})):
            r = http.post(url, **kw)
            assert r.status_code in (400, 403, 415, 422) and mq.holen(mandant, e["id"])["status"] == "blocked", (name, r.status_code, r.text)
        assert http.post(U + "/auftrag/%s/archivieren" % e["id"], data={"confirm": "true"}).status_code in (400, 415, 422)
        for name, kopf in (("DNS rebinding: foreign host and origin", {"Host": "evil.example:9119", "Origin": "http://evil.example:9119"}),
                           ("foreign host without origin", {"Host": "evil.example"})):
            r = http.post(url, json=OK, headers=kopf)
            assert r.status_code == 403 and mq.holen(mandant, e["id"])["status"] == "blocked", (name, r.status_code, r.text)
        assert http.post(url, json=OK, headers={"Origin": "http://127.0.0.1:9119"}).status_code == 200  # same origin (dashboard in a browser)
        assert _warte(mq, mandant, e["id"])["status"] == "done" and os.path.isfile(os.path.join(archiv, "w.txt"))
        os.remove(os.path.join(archiv, "w.txt"))
        disk = _stand(tmp)
        # K1 / K2: archive of a running request -> 409; after the timeout (run presumed dead) it can be archived
        e = mq.anlegen(mandant, "test-client", "sync", {"von": {"quelle": w["id"], "pfad": ""}, "nach": {"quelle": z["id"], "pfad": ""}, "namen": []}, "t", "d")
        mq._setzen(mandant, e["id"], ("blocked",), status="running", freigegeben_ts=time.time())
        assert http.post(U + "/auftrag/%s/archivieren" % e["id"], json=OK).status_code == 409
        mq._setzen(mandant, e["id"], None, freigegeben_ts=time.time() - mq.LAUFZEIT_S - 1)
        assert http.post(U + "/auftrag/%s/archivieren" % e["id"], json=OK).status_code == 200 and mq.holen(mandant, e["id"])["status"] == "archived"
        assert http.post(U + "/auftrag/%s/freigeben" % e["id"], json=OK).status_code == 409 and _stand(tmp) == disk
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
        assert karte and karte[0]["status"] == "blocked" and "MCP test-client" in karte[0]["titel"] and "file the note" in karte[0]["details"], tab
        assert "Items (2): note.txt, sub" in karte[0]["details"] and "Into folder (exact): " + archiv in karte[0]["details"], karte[0]["details"]
        # ---- human release through the backend route -> executed, verified, logged
        rr = http.post(U + "/auftrag/%s/freigeben" % kid, json=OK)
        assert rr.status_code == 200, rr.text
        e = _warte(mq, mandant, kid)
        assert e["status"] == "done", e
        with open(os.path.join(archiv, "note.txt"), encoding="utf-8") as f:
            assert f.read() == "hello trove"
        assert os.path.isfile(os.path.join(archiv, "sub", "deep.txt")) and os.path.isfile(os.path.join(inbox, "note.txt"))
        assert http.post(U + "/auftrag/%s/freigeben" % kid, json=OK).status_code == 409  # only once
        ts, _ = await _ruf(c, "task_status", task_id=kid)
        assert ts["status"] == "done", ts
        # ---- rejected request never runs
        with open(os.path.join(inbox, "late.txt"), "w", encoding="utf-8") as f:
            f.write("late")
        r, err = await _ruf(c, "request_sync", from_source=q["id"], to_source=z["id"])
        assert not err, r
        assert http.post(U + "/auftrag/%s/archivieren" % r["task_id"], json=OK).status_code == 200
        assert mq.holen(mandant, r["task_id"])["status"] == "rejected"
        assert http.post(U + "/auftrag/%s/freigeben" % r["task_id"], json=OK).status_code == 409
        assert not os.path.exists(os.path.join(archiv, "late.txt"))
        # ---- permission re-checked at release: target switched back to read-only meanwhile -> fails, nothing written
        r, err = await _ruf(c, "request_copy", from_source=q["id"], to_source=z["id"], names=["late.txt"])
        assert not err, r
        http.post(U + "/quellen/" + z["id"] + "/schreibbar", json={})
        assert http.post(U + "/auftrag/%s/freigeben" % r["task_id"], json=OK).status_code == 200
        e = _warte(mq, mandant, r["task_id"])
        assert e["status"] == "failed" and "read-only" in e["ergebnis"] and not os.path.exists(os.path.join(archiv, "late.txt")), e
        # ---- into knowledge: also only a request
        r, err = await _ruf(c, "request_into_knowledge", source=q["id"], collection="mcp-test")
        assert not err and r["status"] == "blocked", r
        wiki = os.path.join(os.environ["TROVE_DATEN"], "knowledge")
        assert not os.path.exists(wiki) or not any(f.endswith(".md") for _, _, fs in os.walk(wiki) for f in fs), "nothing before release"
        assert http.post(U + "/auftrag/%s/freigeben" % r["task_id"], json=OK).status_code == 200
        e = _warte(mq, mandant, r["task_id"], 120)
        assert e["status"] == "done", e
        zwillinge = [f for d, _, fs in os.walk(os.path.join(wiki, "raw", "trove", "mcp-test")) for f in fs if f.endswith(".md")]
        assert any(f.endswith("_note.md") for f in zwillinge), zwillinge
        # ---- rename / new folder (only when the backend has them)
        if hasattr(mod, "umbenennen"):
            http.post(U + "/quellen/" + z["id"] + "/schreibbar", json={"nur_lesen": False})
            r, err = await _ruf(c, "request_rename", source=z["id"], name="note.txt", new_name="renamed.txt")
            assert not err and r["status"] == "blocked" and os.path.exists(os.path.join(archiv, "note.txt")), r
            assert http.post(U + "/auftrag/%s/freigeben" % r["task_id"], json=OK).status_code == 200
            assert _warte(mq, mandant, r["task_id"])["status"] == "done"
            assert os.path.exists(os.path.join(archiv, "renamed.txt")) and not os.path.exists(os.path.join(archiv, "note.txt"))
            _, err = await _ruf(c, "request_rename", source=z["id"], name="renamed.txt", new_name="sub")
            assert err  # would replace an existing entry
            _, err = await _ruf(c, "request_rename", source=q["id"], name="note.txt", new_name="x.txt")
            assert err  # read-only source
        if hasattr(mod, "ordner_neu"):
            r, err = await _ruf(c, "request_new_folder", source=z["id"], name="2026")
            assert not err and not os.path.exists(os.path.join(archiv, "2026")), r
            assert http.post(U + "/auftrag/%s/freigeben" % r["task_id"], json=OK).status_code == 200
            assert _warte(mq, mandant, r["task_id"])["status"] == "done" and os.path.isdir(os.path.join(archiv, "2026"))
        # ---- agent task: full text visible in the tab; the Hermes card is created only after release
        r, err = await _ruf(c, "request_agent_task", title="sort the inbox", text="Line one.\nLine two: list duplicates only.")
        assert not err and r["status"] == "blocked", r
        assert not os.path.exists(HERMES_LOG) or "create" not in open(HERMES_LOG).read()
        karte = [a for a in http.get(U + "/auftraege").json()["auftraege"] if a["id"] == r["task_id"]][0]
        assert "Line two: list duplicates only." in karte["details"], karte
        assert http.post(U + "/auftrag/%s/freigeben" % r["task_id"], json=OK).status_code == 200
        assert _warte(mq, mandant, r["task_id"])["status"] == "done"
        aufrufe = open(HERMES_LOG).read()
        assert "kanban create" in aufrufe and "kanban unblock t_fake1" in aufrufe, aufrufe
        # ---- K5: no traceback and no system paths to the client
        import mcp_server
        roh = 'lese_treppe.py fehlgeschlagen (rc 1): Traceback (most recent call last):\n  File "/usr/local/lib/python3/site.py", line 1\nValueError: broken /usr/local/lib/x/y.py'
        sauber = mcp_server._sauber(roh, 500, mod)
        assert "Traceback" not in sauber and "/usr/" not in sauber and "ValueError" in sauber, sauber
        msg, err = await _ruf(c, "read_receipt", source=w["id"], path="kaputt.pdf")
        print("read_receipt on a broken PDF ->", msg if err else "ok")
        assert not err or ("Traceback" not in msg and "/usr/" not in msg and "site-packages" not in msg), msg
        # ---- S3: at most 20 open requests per client
        http.post(U + "/quellen/" + z["id"] + "/schreibbar", json={"nur_lesen": False})
        offen = sum(1 for x in mq.liste(mandant) if x["client"] == "test-client" and x["status"] == "blocked")
        for _ in range(25):
            msg, err = await _ruf(c, "request_sync", from_source=w["id"], to_source=z["id"])
            if err:
                break
            offen += 1
        assert err and "still waiting" in msg and offen == mq.MAX_OFFEN_JE_CLIENT, (offen, msg)
        # K7: a global cap, whatever names the clients report
        for i in range(100):
            try:
                mq.anlegen(mandant, "other-%d" % i, "sync", {"von": {"quelle": w["id"], "pfad": ""}, "nach": {"quelle": z["id"], "pfad": ""}, "namen": []}, "t", "d")
            except ValueError as ex:
                assert "still waiting" in str(ex)
                break
        assert sum(1 for x in mq.liste(mandant) if x["status"] == "blocked") == mq.MAX_OFFEN_GESAMT
        tab = http.get(U + "/auftraege").json()["auftraege"]
        assert sum(1 for a in tab if a["status"] == "blocked" and a["id"].startswith("mcp-")) == mq.MAX_OFFEN_GESAMT
    # ---- receipt log: every MCP step with the client's name, chain intact
    pf = bp.pfad_fuer(mandant)
    alle = list(bp.eintraege(pf))
    mcp = [x for x in alle if x.get("werkzeug") == "trove-mcp"]
    texte = " | ".join(x.get("meldung", "") for x in mcp)
    for teil in ("refused before queueing", "created (blocked", "released by a human", kid + " from test-client done", "rejected by a human", "failed", "check_changes"):
        assert teil in texte, (teil, texte)
    assert all(x["mcp_client"] == "test-client" or x["mcp_client"].startswith("other-") for x in mcp if "mcp_client" in x), "client names in the log"
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
        # S1: DNS-rebinding protection is set explicitly: foreign Host or any browser Origin is refused
        import httpx
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                                              "clientInfo": {"name": "raw", "version": "1"}}}
        kopf = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        url = "http://127.0.0.1:%d/mcp" % port
        with httpx.Client(timeout=10) as h:
            gut = h.post(url, json=init, headers=kopf)
            assert gut.status_code == 200, (gut.status_code, gut.text[:200])
            for name, extra in (("foreign host", {"Host": "evil.example:%d" % port}), ("foreign origin", {"Origin": "http://evil.example"}),
                                ("localhost origin of another app", {"Origin": "http://localhost:3000"})):
                r = h.post(url, json=init, headers={**kopf, **extra})
                assert r.status_code in (403, 421), (name, r.status_code)
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
        for d in ("work/wdir", "work/wsub", "outside"):
            os.makedirs(os.path.join(tmp, d))
        for rel, inhalt in (("work/w.txt", "W"), ("work/wdir/x.txt", "X"), ("work/wsub/y.txt", "Y"), ("outside/secret.txt", "SECRET"), ("work/kaputt.pdf", "no pdf")):
            with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
                f.write(inhalt)
        os.symlink(os.path.join(tmp, "outside", "secret.txt"), os.path.join(tmp, "work", "aussen"))
        os.symlink(os.path.join(tmp, "outside"), os.path.join(tmp, "work", "aussendir"))
        os.symlink(".", os.path.join(tmp, "work", "self"))  # link to an ancestor inside the source (B2)
        os.symlink("wdir", os.path.join(tmp, "work", "dlink"))
        for d in ("work/wdir/deep", "tgt", "tgt2/wdir", "ro", "outside2"):
            os.makedirs(os.path.join(tmp, d), exist_ok=True)
        for rel, inhalt in (("work/wdir/deep/z.txt", "Z"), ("ro/keep.txt", "K")):
            with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
                f.write(inhalt)
        os.symlink(os.path.join(tmp, "ro"), os.path.join(tmp, "tgt", "wdir"))  # target link to a read-only source (B3)
        os.symlink(os.path.join(tmp, "outside2"), os.path.join(tmp, "tgt2", "wdir", "deep"))  # nested target link to outside
        global HERMES_LOG
        HERMES_LOG = os.path.join(tmp, "hermes-calls.log")
        fake = os.path.join(tmp, "hermes")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(FAKE_HERMES % (sys.executable, HERMES_LOG))
        os.chmod(fake, 0o755)
        os.environ["TROVE_HERMES"] = fake
        dash = os.path.join(plugin, "dashboard")
        spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_trove", os.path.join(dash, "plugin_api.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(mod.router, prefix=U)
        http = TestClient(app, base_url="http://127.0.0.1:9119")  # the dashboard is reached on the loopback address
        env = dict(os.environ, PYTHONPATH="")
        server_py = os.path.join(dash, "mcp_server.py")
        asyncio.run(stdio_teil(server_py, env, http, mod, tmp))
        asyncio.run(http_teil(server_py, env))
        # K6: without the mcp package -> one clear line, no traceback
        r = subprocess.run([sys.executable, "-c", "import sys, runpy; sys.modules['mcp'] = None; sys.argv = ['mcp_server.py']; "
                            "runpy.run_path(%r, run_name='__main__')" % server_py], capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode != 0 and "pip install mcp" in r.stderr and "Traceback" not in r.stderr, r.stderr
        print("mcp ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
