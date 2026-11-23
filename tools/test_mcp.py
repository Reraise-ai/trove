#!/usr/bin/env python3
"""MCP access test: starts plugin/dashboard/mcp_server.py as a real MCP stdio server (and once over HTTP on 127.0.0.1),
talks to it with the MCP SDK client and checks both positions of the switch `freigabe_pflicht`:
ON  - an MCP client can read, but every change is only a BLOCKED request that runs after Release through the Trove tab's
      backend route, never through MCP itself (the long section in stdio_teil);
OFF (the default) - tab cards and MCP requests start at once, folder rights still apply (aus_teil), and the tab routes keep
      their protection against foreign web pages in both positions (web_teil);
plus the races of auftraege.json, a repeated create and partial failures after `kanban create` (rennen_teil).
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
            if not os.path.islink(p) and f not in ("hermes", "hermes.exe") and not f.startswith("hermes-calls.log"):
                with open(p, "rb") as h:
                    out[os.path.relpath(p, wurzel)] = h.read()
    return out


FAKE_HERMES = """#!%s
import json, os, sys, time
log = %r
with open(log, "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
a = sys.argv[1:]
def flag(n):
    return os.path.exists(log + "." + n)
if a[:2] == ["kanban", "create"]:
    n = sum(1 for z in open(log) if z.startswith("kanban create"))
    if flag("slowcreate"):
        time.sleep(0.4)
    try:
        with open(a[a.index("--body-file") + 1], encoding="utf-8") as f:
            body = f.read().split("\\n")[0]
    except OSError:
        body = "BODYFILE-MISSING"
    with open(log + ".bodies", "a") as f:
        f.write(body + "\\n")
    print(json.dumps({"id": ("t_p%%d" %% os.getpid()) if flag("slowcreate") else ("t_fake%%d" %% n)}))
elif a[:2] == ["kanban", "ls"]:
    while flag("holdls") and not flag("lsgo"):  # the test decides when the board answers (no clock)
        time.sleep(0.02)
    print(open(log + ".lsjson").read() if flag("lsjson") else "[]")
elif a[:2] == ["kanban", "unblock"] and flag("failunblock"):
    sys.stderr.write("cannot unblock this card")
    sys.exit(1)
else:
    print("{}")
"""


def _fake_hermes(ordner):
    """Fake hermes CLI. POSIX: executable script with shebang. Windows cannot start a shebang script (WinError 193); the real
    hermes there is an .exe, so the fake is one too: pip's console-script launcher (distlib t64.exe) + shebang + zip with __main__.py,
    the same way pip builds console scripts -- arguments reach the fake exactly as CreateProcess passes them."""
    quelltext = FAKE_HERMES % (sys.executable, HERMES_LOG)
    if os.name != "nt":
        fake = os.path.join(ordner, "hermes")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(quelltext)
        os.chmod(fake, 0o755)
        return fake
    import io
    import pkgutil
    import platform
    import zipfile
    launcher = pkgutil.get_data("pip._vendor.distlib", "t64-arm.exe" if platform.machine().lower() == "arm64" else "t64.exe")
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as z:
        z.writestr("__main__.py", quelltext)
    fake = os.path.join(ordner, "hermes.exe")
    with open(fake, "wb") as f:
        f.write(launcher + b'#!"' + sys.executable.encode("utf-8") + b'"\r\n' + puffer.getvalue())
    return fake


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
        offen = sum(1 for x in mq.liste(mandant) if x["client"] == "test-client" and mq._offen(x, time.time()))
        for _ in range(25):
            msg, err = await _ruf(c, "request_sync", from_source=w["id"], to_source=z["id"])
            if err:
                break
            offen += 1
        assert err and "still open" in msg and offen == mq.MAX_OFFEN_JE_CLIENT, (offen, msg)
        # K7: a global cap, whatever names the clients report
        for i in range(100):
            try:
                mq.anlegen(mandant, "other-%d" % i, "sync", {"von": {"quelle": w["id"], "pfad": ""}, "nach": {"quelle": z["id"], "pfad": ""}, "namen": []}, "t", "d")
            except ValueError as ex:
                assert "still open" in str(ex)
                break
        offen = [x for x in mq.liste(mandant) if mq._offen(x, time.time())]
        assert len(offen) == mq.MAX_OFFEN_GESAMT, len(offen)
        tab = http.get(U + "/auftraege").json()["auftraege"]
        assert sum(1 for a in tab if a["status"] == "blocked" and a["id"].startswith("mcp-")) == sum(1 for x in offen if x["status"] == "blocked")
    # ---- receipt log: every MCP step with the client's name, chain intact
    pf = bp.pfad_fuer(mandant)
    alle = list(bp.eintraege(pf))
    mcp = [x for x in alle if x.get("werkzeug") == "trove-mcp"]
    texte = " | ".join(x.get("meldung", "") for x in mcp)
    for teil in ("refused before queueing", "queued: ", "released in the Trove tab", kid + " from test-client done", "rejected in the Trove tab", "failed", "check_changes"):
        assert teil in texte, (teil, texte)
    assert "by a human" not in texte, texte  # no click is proven by the log: it says "in the Trove tab", not "by a human"
    assert all(x["mcp_client"] == "test-client" or x["mcp_client"].startswith("other-") for x in mcp if "mcp_client" in x), "client names in the log"
    assert any(x["ereignis"] == "verifiziert" and x.get("ziel", "").endswith("note.txt") for x in alle)
    pr = bp.pruefen(pf)
    assert pr.get("ok", True) is not False, pr


def _n_create():
    with open(HERMES_LOG, encoding="utf-8") as f:
        return sum(1 for z in f if z.startswith("kanban create"))


def _aufrufe():
    with open(HERMES_LOG, encoding="utf-8") as f:
        return f.read()


def _n_ls():
    return sum(1 for z in _aufrufe().splitlines() if z.startswith("kanban ls"))


def _warte_ls(n0):
    """Until the fake hermes got one more `kanban ls` than n0: the list has read auftraege.json and waits for the board."""
    ende = time.time() + 30
    while _n_ls() <= n0:
        assert time.time() < ende, "kanban ls was not called"
        time.sleep(0.02)


def _gehalten(pfad):
    """Is the lock file pfad held right now (by anyone)?"""
    import belegprotokoll as bp
    s = bp.Sperre(pfad)
    try:
        s.sperren()
    except bp.ProtokollGesperrt:
        return True
    s.entsperren()
    return False


def _flag(name, an=True, inhalt=""):
    p = HERMES_LOG + "." + name
    if an:
        with open(p, "w", encoding="utf-8") as f:
            f.write(inhalt)
    elif os.path.exists(p):
        os.remove(p)


def _bodies():
    p = HERMES_LOG + ".bodies"
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [z.rstrip("\n") for z in f]


def _karten(mod):
    return {a["id"]: a for a in mod._auftraege_lesen(mod.MANDANT)}


def _karten_roh(mod):
    """auftraege.json without taking the lock (for a test that holds it)."""
    return {a["id"]: a for a in mod._auftraege_roh(mod.MANDANT)}


def _log_texte(mandant, werkzeug=None):
    import belegprotokoll as bp
    return [x.get("meldung", "") for x in bp.eintraege(bp.pfad_fuer(mandant)) if werkzeug is None or x.get("werkzeug") == werkzeug]


# what Hermes Desktop sends for ctx.rest (hermes-agent apps/desktop: pluginRest -> IPC hermes:api -> Node http.request from the main
# process): Host 127.0.0.1:<port>, Content-Type JSON, the session token header, and NO Origin header
DESKTOP_KOPF = {"Host": "127.0.0.1:54321", "X-Hermes-Session-Token": "token-from-desktop", "Content-Type": "application/json"}
FREMD = {"Host": "evil.example:9119", "Origin": "http://evil.example:9119"}


def web_teil(http, mod):
    """The three tab routes (create, release, archive) in BOTH positions of the switch: foreign host / foreign origin / forms are
    refused (403 names host and origin), the Hermes Desktop request is accepted, and the switch decides only whether a card starts."""
    for pflicht in ("0", "1"):
        os.environ["TROVE_FREIGABE_PFLICHT"] = pflicht
        wo = "switch " + ("ON" if pflicht == "1" else "OFF")
        st = http.get(U + "/status").json()
        assert st["freigabe_pflicht"] is (pflicht == "1") and st["freigabe_pflicht_quelle"] == ("umgebung" if pflicht == "1" else "standard"), (wo, st)
        assert http.get(U + "/auftraege").json()["freigabe_pflicht"] is (pflicht == "1"), wo  # the tab's 20-s refresh carries the switch
        def neu(titel, **kopf):
            return http.post(U + "/auftrag", json={"titel": titel, "text": "x", "confirm": True}, headers=kopf or None)
        # --- create: Hermes Desktop headers -> 200; the switch decides blocked / started
        vorher = _n_create()
        r = neu("web-" + pflicht, **DESKTOP_KOPF)
        assert r.status_code == 200 and _n_create() == vorher + 1, (wo, r.status_code, r.text)
        k = r.json()
        assert k["status"] == ("blocked" if pflicht == "1" else "started") and "quelle" not in k and k["herkunft"] == "tab", (wo, k)
        assert (("kanban unblock %s" % k["id"]) in _aufrufe()) == (pflicht == "0"), wo  # started = unblocked, no click
        assert _karten(mod)[k["id"]]["status"] == k["status"], wo  # stored status = returned status
        # --- foreign pages: refused on all three routes, nothing created / released / archived
        for route, kw in (("/auftrag", {"json": {"titel": "evil", "confirm": True}}),
                          ("/auftrag/%s/freigeben" % k["id"], {"json": OK}), ("/auftrag/%s/archivieren" % k["id"], {"json": OK})):
            n0, a0 = _n_create(), _aufrufe()
            for name, kopf, code in (("foreign host+origin", FREMD, 403), ("foreign host only", {"Host": "evil.example"}, 403),
                                     ("foreign origin", {"Origin": "http://evil.example"}, 403), ("Origin: null", {"Origin": "null"}, 403),
                                     ("local app on another port", {"Origin": "http://localhost:3000"}, 403),
                                     ("loopback, other port", {"Origin": "http://127.0.0.1:3000"}, 403)):
                r = http.post(U + route, headers=kopf, **kw)
                assert r.status_code == code, (wo, route, name, r.status_code, r.text)
                d = r.json()["detail"]
                assert ("evil.example" in d if "Host" in kopf else True) and (kopf["Origin"] in d if "Origin" in kopf else "origin 'none'" in d), (wo, route, name, d)
            for name, kw2 in (("urlencoded form", {"data": {"confirm": "true", "titel": "evil"}}), ("multipart form", {"files": {"confirm": (None, "true")}}),
                              ("text/plain", {"content": b'{"confirm": true, "titel": "evil"}', "headers": {"Content-Type": "text/plain"}})):
                r = http.post(U + route, **kw2)
                assert r.status_code in (415, 422), (wo, route, name, r.status_code, r.text)
            assert _n_create() == n0 and _aufrufe() == a0, (wo, route, "refused requests must not reach hermes")
        # --- release and archive need {"confirm": true} for EVERY card (also t_...), in both positions
        for route in ("/auftrag/%s/freigeben" % k["id"], "/auftrag/%s/archivieren" % k["id"]):
            for kw in ({}, {"json": {}}, {"json": {"confirm": False}}):
                assert http.post(U + route, **kw).status_code in (400, 415, 422), (wo, route, kw)
        # --- release / archive with the Desktop headers; release only for a blocked card (switch OFF: it was started already)
        a0 = _aufrufe()
        r = http.post(U + "/auftrag/%s/freigeben" % k["id"], json=OK, headers=DESKTOP_KOPF)
        if pflicht == "1":
            assert r.status_code == 200 and "fehler" not in r.json(), (wo, r.text)
            assert _karten(mod)[k["id"]]["status"] == "started" and "kanban unblock %s" % k["id"] in _aufrufe()[len(a0):], wo
        else:
            assert r.status_code == 409 and _aufrufe() == a0 and _karten(mod)[k["id"]]["status"] == "started", (wo, r.status_code, r.text)
        # --- a task id that the hermes command line would read as an option: 400, hermes is not called
        for route in ("/auftrag/--help/freigeben", "/auftrag/--help/archivieren"):
            a0 = _aufrufe()
            r = http.post(U + route, json=OK, headers=DESKTOP_KOPF)
            assert r.status_code == 400 and _aufrufe() == a0, (wo, route, r.status_code, r.text)
        r = http.post(U + "/auftrag/%s/archivieren" % k["id"], json=OK, headers=DESKTOP_KOPF)
        assert r.status_code == 200 and _karten(mod)[k["id"]]["status"] == "archived", (wo, r.text)
    # --- allow-list: another host name is refused until it is configured; the refusal says how
    os.environ["TROVE_FREIGABE_PFLICHT"] = "0"
    kopf = dict(DESKTOP_KOPF, Host="hermes.example.test")
    r = http.post(U + "/auftrag", json={"titel": "proxy", "confirm": True}, headers=kopf)
    assert r.status_code == 403 and "hermes.example.test" in r.json()["detail"] and "TROVE_DASHBOARD_HOSTS" in r.json()["detail"], r.text
    os.environ["TROVE_DASHBOARD_HOSTS"] = "hermes.example.test"
    try:
        assert http.post(U + "/auftrag", json={"titel": "proxy", "confirm": True}, headers=kopf).status_code == 200
        os.environ["TROVE_DASHBOARD_ORIGIN"] = "https://hermes.example.test"
        assert http.post(U + "/auftrag", json={"titel": "proxy2", "confirm": True}, headers=dict(kopf, Origin="https://hermes.example.test")).status_code == 200
    finally:
        os.environ.pop("TROVE_DASHBOARD_HOSTS", None)
        os.environ.pop("TROVE_DASHBOARD_ORIGIN", None)
    r = http.post(U + "/auftrag", json={"titel": "ipv6 loopback", "confirm": True}, headers={"Host": "[::1]:54321", "Content-Type": "application/json"})
    assert r.status_code == 200, r.text  # Hermes on the IPv6 loopback address
    assert http.post(U + "/auftrag", json={"titel": "bad id", "client_id": "x y", "confirm": True}).status_code == 400
    assert http.post(U + "/auftrag", json={"titel": "no confirm needed to start"}).status_code == 200  # switch OFF: no confirmation step for the start
    print("web ok")


def rennen_teil(http, mod):
    """Races and partial failures of the card list: unique body files, one writer for auftraege.json, a repeated create returns the
    existing card, and nothing after a successful `kanban create` ends in an error."""
    import threading
    import belegprotokoll as bp
    mandant = mod.MANDANT
    os.environ["TROVE_FREIGABE_PFLICHT"] = "0"
    fehler = []

    def thread(fn):
        def lauf():
            try:
                fn()
            except BaseException as ex:  # noqa: BLE001 - the test reports it
                fehler.append(repr(ex))
        return threading.Thread(target=lauf)

    def alle(threads):
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not fehler, fehler

    # (a) two creates at the same moment: each card gets its own text, both are in auftraege.json, no body file is left over
    _flag("slowcreate")
    b0, erg = len(_bodies()), {}
    alle([thread(lambda i=i: erg.__setitem__(i, mod._auftrag_anlegen({"titel": "Karte %d" % i, "text": "TEXT-%d" % i}, "tab", True))) for i in (1, 2)])
    assert sorted(_bodies()[b0:]) == ["TEXT-1", "TEXT-2"], _bodies()[b0:]
    karten = _karten(mod)
    assert erg[1]["id"] != erg[2]["id"] and karten[erg[1]["id"]]["titel"] == "Karte 1" and karten[erg[2]["id"]]["titel"] == "Karte 2" and "fehler" not in erg[1] and "fehler" not in erg[2]
    assert not [f for f in os.listdir(mod._mandant_dir(mandant)) if f.startswith("auftrag_")], os.listdir(mod._mandant_dir(mandant))
    # (b) a create while the list (GET /auftraege) is fetching the board: the new card stays in auftraege.json
    _flag("holdls")
    n_ls = _n_ls()
    liste = thread(lambda: http.get(U + "/auftraege"))
    liste.start()
    _warte_ls(n_ls)  # the list has read auftraege.json and now waits for the board: create the card exactly now
    neu = mod._auftrag_anlegen({"titel": "waehrend Liste", "text": "L"}, "tab", True)
    _flag("lsgo")
    liste.join()
    assert not fehler, fehler
    assert neu["id"] in _karten(mod) and _karten(mod)[neu["id"]]["status"] != "archived", _karten(mod).get(neu["id"])
    _flag("holdls", an=False)
    _flag("lsgo", an=False)
    # (c) archive and create at the same time: both are effective
    alt = erg[1]["id"]
    ergc = {}
    alle([thread(lambda: ergc.update(http.post(U + "/auftrag/%s/archivieren" % alt, json=OK).json())), thread(lambda: ergc.update(c=mod._auftrag_anlegen({"titel": "waehrend Archiv", "text": "A"}, "tab", True)))])
    assert _karten(mod)[alt]["status"] == "archived" and ergc["c"]["id"] in _karten(mod)
    _flag("slowcreate", an=False)
    # (d) the single writer: 8 threads x 5 changes, none lost
    def schreiber(n):
        for i in range(5):
            mod._auftraege_aendern(mandant, lambda liste, i=i: (time.sleep(0.002), liste.append({"id": "z_%d_%d" % (n, i), "status": "blocked"})))
    alle([thread(lambda n=n: schreiber(n)) for n in range(8)])
    assert sum(1 for kid in _karten(mod) if kid.startswith("z_")) == 40
    assert not [f for f in os.listdir(mod._mandant_dir(mandant)) if f.endswith(".tmp") or f.endswith(".neu")]
    # (e) the same client_id twice at the same moment (double click): one card
    _flag("slowcreate")
    n0, res = _n_create(), []
    alle([thread(lambda: res.append(mod._auftrag_anlegen({"titel": "doppelt", "client_id": "form-0001-abcdef"}, "tab", True))) for _ in range(2)])
    assert _n_create() == n0 + 1 and res[0]["id"] == res[1]["id"] and sum(1 for r in res if r.get("wiederholt")) == 1, res
    _flag("slowcreate", an=False)
    # (f) the start fails after the card exists: HTTP 200 with "fehler", card blocked and listed; the repeat (same client_id) creates nothing
    _flag("failunblock")
    n0 = _n_create()
    kopf = {"Host": "127.0.0.1:9119"}
    r = http.post(U + "/auftrag", json={"titel": "Startfehler", "text": "s", "client_id": "form-0002-abcdef", "confirm": True}, headers=kopf)
    k = r.json()
    assert r.status_code == 200 and k["status"] == "blocked" and "start failed" in k["fehler"] and _n_create() == n0 + 1, (r.status_code, k)
    assert _karten(mod)[k["id"]]["status"] == "blocked" and "start failed" in _karten(mod)[k["id"]]["fehler"]
    r = http.post(U + "/auftrag", json={"titel": "Startfehler", "text": "s", "client_id": "form-0002-abcdef", "confirm": True}, headers=kopf)
    assert r.status_code == 200 and r.json()["id"] == k["id"] and r.json().get("wiederholt") and _n_create() == n0 + 1, r.text
    _flag("failunblock", an=False)
    r = http.post(U + "/auftrag/%s/freigeben" % k["id"], json=OK, headers=kopf)  # "Release" is the retry
    assert r.status_code == 200 and _karten(mod)[k["id"]]["status"] == "started" and _karten(mod)[k["id"]].get("fehler") is None, r.text
    # (g) the receipt log is busy: a short hold is waited for; a hold that does not end still reports the card (200 + fehler), once
    pf = bp.pfad_fuer(mandant)
    halter = threading.Event()
    def halten():
        with bp.Protokoll(pf):
            halter.set()
            time.sleep(1.2)
    t = threading.Thread(target=halten)
    t.start()
    halter.wait(5)
    n0 = _n_create()
    r = mod._auftrag_anlegen({"titel": "Protokoll belegt", "text": "p"}, "tab", True)
    t.join()
    assert "fehler" not in r and _n_create() == n0 + 1 and any("Protokoll belegt" in z for z in _log_texte(mandant)), r
    echt = mod.mcp_queue.log
    def belegt(*a, **k):
        raise bp.ProtokollGesperrt("busy")
    mod.mcp_queue.log = belegt
    try:
        r = http.post(U + "/auftrag", json={"titel": "Protokoll dauerhaft belegt", "client_id": "form-0003-abcdef", "confirm": True}, headers=kopf)
    finally:
        mod.mcp_queue.log = echt
    k = r.json()
    assert r.status_code == 200 and "receipt-log" in k["fehler"] and k["id"] in _karten(mod) and _n_create() == n0 + 2, (r.status_code, k)
    # (h) list: a finished card is logged once; a busy log takes the claim back and the next check logs it
    kid = k["id"]
    _flag("lsjson", inhalt=json.dumps([{"id": kid, "status": "done", "result": "RESULT-OK"}]))
    mod.mcp_queue.log = echt
    echt_s = bp.schreiben
    def schreiben_belegt(*a, **kw):
        raise bp.ProtokollGesperrt("busy")
    bp.schreiben = schreiben_belegt
    try:
        assert http.get(U + "/auftraege").status_code == 200
    finally:
        bp.schreiben = echt_s
    assert _karten(mod)[kid]["status"] == "done" and _karten(mod)[kid]["protokolliert"] == [], _karten(mod)[kid]
    for _ in range(2):
        assert http.get(U + "/auftraege").status_code == 200
    assert sum(1 for z in _log_texte(mandant) if "Task result (done): RESULT-OK" in z) == 1 and _karten(mod)[kid]["protokolliert"] == ["done"]
    _flag("lsjson", an=False)
    # (i) the same client_id with other content: 409, nothing created (the old card is not handed back as if it were the new text)
    n0 = _n_create()
    r = http.post(U + "/auftrag", json={"titel": "Startfehler", "text": "neuer Text", "client_id": "form-0002-abcdef", "confirm": True}, headers=kopf)
    assert r.status_code == 409 and "different content" in r.json()["detail"] and _n_create() == n0, (r.status_code, r.text)
    # (j) archive while the list fetches the board: the card stays archived (no flip back to the board state read before)
    k = mod._auftrag_anlegen({"titel": "Archiv waehrend ls", "text": "a"}, "tab", True)
    _flag("lsjson", inhalt=json.dumps([{"id": k["id"], "status": "running"}]))
    _flag("holdls")
    n_ls = _n_ls()
    liste = thread(lambda: http.get(U + "/auftraege"))
    liste.start()
    _warte_ls(n_ls)  # the board answer ("running") is still to come: archive exactly now
    assert http.post(U + "/auftrag/%s/archivieren" % k["id"], json=OK).status_code == 200
    _flag("lsgo")
    liste.join()
    assert not fehler, fehler
    assert _karten(mod)[k["id"]]["status"] == "archived", _karten(mod)[k["id"]]
    _flag("holdls", an=False)
    _flag("lsgo", an=False)
    _flag("lsjson", an=False)
    # (k) lock of auftraege.json: a crashed holder blocks nothing; two waiters never hold it together; a living holder -> 503, not 500
    sperre = mod._auftraege_pfad(mandant) + ".lock"
    waise = subprocess.run([sys.executable, "-c", "import os, sys; sys.path.insert(0, %r); import belegprotokoll as bp; "
                            "bp.WarteSperre(%r).sperren(); os._exit(9)" % (os.path.dirname(mod.__file__), sperre)], timeout=60)
    assert waise.returncode == 9 and os.path.exists(sperre)
    t0 = time.monotonic()
    r = http.get(U + "/auftraege")
    assert r.status_code == 200 and time.monotonic() - t0 < 1, (r.status_code, time.monotonic() - t0)
    halter, hoechst, vorher = [0], [0], len(_karten(mod))
    zaehler = threading.Lock()
    def eintragen(n):
        def fn(liste):
            with zaehler:
                halter[0] += 1
                hoechst[0] = max(hoechst[0], halter[0])
            time.sleep(0.01)
            liste.append({"id": "w_%d" % n, "status": "blocked"})
            with zaehler:
                halter[0] -= 1
        mod._auftraege_aendern(mandant, fn)
    alle([thread(lambda n=n: eintragen(n)) for n in range(6)])
    assert hoechst[0] == 1 and len(_karten(mod)) == vorher + 6, (hoechst, len(_karten(mod)), vorher)
    belegt, frei = threading.Event(), threading.Event()
    def halten_liste():
        with mod.bp.WarteSperre(sperre):
            belegt.set()
            frei.wait(30)
    t = threading.Thread(target=halten_liste)
    t.start()
    belegt.wait(5)
    alt_warten = mod.bp.WarteSperre.warten_s
    mod.bp.WarteSperre.warten_s = 0.3  # instead of 20 s
    try:
        n0, a0 = _n_create(), _aufrufe()
        blockiert = next(kid for kid, a in _karten_roh(mod).items() if a.get("status") == "blocked")
        for wie, route, kw in (("list", "get", {}), ("create", "post", {"json": {"titel": "waehrend Sperre", "confirm": True}}),
                               ("release", "post", {"json": OK})):
            url = U + {"list": "/auftraege", "create": "/auftrag", "release": "/auftrag/%s/freigeben" % blockiert}[wie]
            r = getattr(http, route)(url, **kw)
            assert r.status_code == 503 and "busy" in r.json()["detail"], (wie, r.status_code, r.text)
        assert _n_create() == n0 and _aufrufe() == a0, "nothing reached hermes while the list was busy"
    finally:
        mod.bp.WarteSperre.warten_s = alt_warten
        frei.set()
        t.join()
    assert http.get(U + "/auftraege").status_code == 200
    print("rennen ok")


async def aus_teil(server_py, env, http, mod, tmp):
    """Switch OFF (default): an MCP request starts at once, with no click; folder rights still apply; the switch is read on every call."""
    from mcp import Client, StdioServerParameters
    from mcp.types import Implementation
    import mcp_queue as mq
    import belegprotokoll as bp
    mandant = mod.MANDANT
    inbox, archiv = os.path.join(tmp, "inbox"), os.path.join(tmp, "archive")
    quellen = {x["name"]: x for x in http.get(U + "/quellen").json()["quellen"]}
    q, z = quellen["Inbox"], quellen["Archive"]
    http.post(U + "/quellen/" + z["id"] + "/schreibbar", json={"nur_lesen": False})
    konfig_datei = os.path.join(tmp, "schalter-trove.json")
    def schalter(an):
        with open(konfig_datei, "w", encoding="utf-8") as f:
            json.dump({"freigabe_pflicht": an}, f)
    for e in mq.liste(mandant):  # the switch-ON section ended at the request caps: clear the waiting requests
        if e["status"] == "blocked":
            mq.ablehnen(mandant, e["id"])
    schalter(False)
    env2 = {k: v for k, v in env.items() if k != "TROVE_FREIGABE_PFLICHT"}
    env2["TROVE_KONFIG"] = konfig_datei  # the MCP server process reads the switch from trove.json on every call
    params = StdioServerParameters(command=sys.executable, args=[server_py], env=env2)
    with open(os.path.join(inbox, "aus.txt"), "w", encoding="utf-8") as f:
        f.write("started without a click")
    async with Client(params, client_info=Implementation(name="test-client", version="1")) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert "Starts at once" in tools["request_copy"].description and "Waits for a human" not in tools["request_copy"].description
        st, _ = await _ruf(c, "status")
        assert st["freigabe_pflicht"] is False and st["freigabe_pflicht_quelle"] == "trove.json" and "starts right after" in st["writes"], st
        assert st["freigabe_pflicht_datei"] == konfig_datei, st  # the file the MCP process reads (TROVE_KONFIG)
        # copy: running at once, done without anyone releasing it
        r, err = await _ruf(c, "request_copy", from_source=q["id"], to_source=z["id"], names=["aus.txt"], reason="no click")
        assert not err and r["status"] == "running" and "Starts at once" in r["note"], r
        e = _warte(mq, mandant, r["task_id"])
        assert e["status"] == "done" and e.get("freigegeben"), e
        with open(os.path.join(archiv, "aus.txt"), encoding="utf-8") as f:
            assert f.read() == "started without a click"
        # the tab shows it as finished, nothing waits for Release
        tab = http.get(U + "/auftraege").json()["auftraege"]
        assert [a["status"] for a in tab if a["id"] == r["task_id"]] == ["done"] and not [a for a in tab if a["id"].startswith("mcp-") and a["status"] == "blocked"], tab
        # folder rights still apply: read-only target -> refused up front, nothing queued, nothing written
        http.post(U + "/quellen/" + z["id"] + "/schreibbar", json={})
        n_queue = len(mq.liste(mandant))
        msg, err = await _ruf(c, "request_copy", from_source=q["id"], to_source=z["id"], names=["note.txt"])
        assert err and "read-only" in msg and len(mq.liste(mandant)) == n_queue and not os.path.exists(os.path.join(archiv, "note.txt")), msg
        http.post(U + "/quellen/" + z["id"] + "/schreibbar", json={"nur_lesen": False})
        # agent task: card created and started, origin and request id recorded
        n0 = _n_create()
        r, err = await _ruf(c, "request_agent_task", title="aus: sort", text="Line one.")
        assert not err and r["status"] == "running", r
        assert _warte(mq, mandant, r["task_id"])["status"] == "done" and _n_create() == n0 + 1
        karte = [a for a in mod._auftraege_lesen(mandant) if a.get("client_id") == r["task_id"]]
        assert len(karte) == 1 and karte[0]["herkunft"] == "mcp:test-client" and karte[0]["status"] == "started", karte
        assert "kanban unblock %s" % karte[0]["id"] in _aufrufe()
        # agent task whose start fails (unblock refused): the request ends failed, not done
        _flag("failunblock")
        try:
            r, err = await _ruf(c, "request_agent_task", title="aus: start fails", text="x")
            assert not err and r["status"] == "running", r
            e = _warte(mq, mandant, r["task_id"])
            assert e["status"] == "failed" and "start failed" in e["ergebnis"], e
        finally:
            _flag("failunblock", an=False)
        # the switch is read on every call: turned on in trove.json, the next request waits blocked
        schalter(True)
        r, err = await _ruf(c, "request_sync", from_source=q["id"], to_source=z["id"])
        assert not err and r["status"] == "blocked" and "Waits for a human" in r["note"], r
        assert http.post(U + "/auftrag/%s/archivieren" % r["task_id"], json=OK).status_code == 200
        schalter(False)
    # ---- the limits count running requests too, with the switch off (a hanging run keeps its place until LAUFZEIT_S)
    import threading
    frei = threading.Semaphore(0)

    class LangsamApi:
        def aktion(self, body):
            frei.acquire(timeout=60)
            return {"lauf": "langsam"}

    leer = {"von": {"quelle": q["id"], "pfad": ""}, "nach": {"quelle": z["id"], "pfad": ""}, "namen": []}

    def neu_offen():
        return mq.anlegen(mandant, "flut-client", "sync", leer, "flut", "d")

    def abgelehnt():
        try:
            neu_offen()
        except ValueError as ex:
            assert "still open (waiting, running, or an agent task finished less than 30 min ago; archiving does not free it)" in str(ex), ex
            return True
        return False

    def laufend():
        return [e["id"] for e in mq.liste(mandant) if e["client"] == "flut-client" and e["status"] == "running"]

    ids = []
    for _ in range(mq.MAX_OFFEN_JE_CLIENT):
        e = neu_offen()
        mq.freigeben(mandant, e["id"], LangsamApi(), direkt=True)
        ids.append(e["id"])
    assert len(laufend()) == mq.MAX_OFFEN_JE_CLIENT and abgelehnt()
    frei.release()  # one run ends -> the next request is accepted
    ende = time.time() + 30
    while len(laufend()) != mq.MAX_OFFEN_JE_CLIENT - 1 and time.time() < ende:
        time.sleep(0.05)
    e = neu_offen()
    assert abgelehnt() and len(laufend()) == mq.MAX_OFFEN_JE_CLIENT - 1  # 19 running + 1 waiting = full again
    mq._setzen(mandant, laufend()[0], None, freigegeben_ts=time.time() - mq.LAUFZEIT_S - 5)  # presumed dead: no longer counts
    assert not abgelehnt()
    for x in mq.liste(mandant):
        if x["client"] == "flut-client" and x["status"] == "blocked":
            mq.ablehnen(mandant, x["id"])
    for _ in ids:
        frei.release()
    ende = time.time() + 30
    while laufend() and time.time() < ende:
        time.sleep(0.05)
    # ---- agent task and the working folder (TROVE_ARBEITSORDNER): checked at every start (tab create, Release, MCP), and Hermes
    # gets exactly the resolved folder that was checked
    quellen = {x["name"]: x for x in http.get(U + "/quellen").json()["quellen"]}
    assert quellen["Inbox"].get("nur_lesen", True) and not quellen["Archive"]["nur_lesen"], quellen
    weg = os.path.join(archiv, "weg")
    os.symlink(os.path.join(tmp, "outside"), weg)  # a link inside the writable source that leads outside
    archiv2 = os.path.join(tmp, "archive2")  # prefix sibling of the writable source "archive"
    os.makedirs(archiv2)
    os.makedirs(os.path.join(archiv, "arbeit"))
    zeiger = os.path.join(archiv, "zeiger")
    os.symlink(os.path.join(archiv, "arbeit"), zeiger)  # a link that stays inside the writable source: Hermes gets its target
    relativ = os.path.relpath(os.path.join(archiv, "arbeit"))  # relative to the current directory of this process
    os.environ["TROVE_FREIGABE_PFLICHT"] = "0"
    try:
        for ordner, code, workspace in ((inbox, 403, None), (archiv, 200, "dir:" + os.path.realpath(archiv)), (os.path.join(tmp, "outside"), 403, None),
                                        (weg, 403, None), (archiv2, 403, None), (relativ, 200, "dir:" + os.path.realpath(os.path.join(archiv, "arbeit"))),
                                        (zeiger, 200, "dir:" + os.path.realpath(os.path.join(archiv, "arbeit"))), (None, 200, "scratch")):
            if ordner:
                os.environ["TROVE_ARBEITSORDNER"] = ordner
            else:
                os.environ.pop("TROVE_ARBEITSORDNER", None)
            n0 = _n_create()
            r = http.post(U + "/auftrag", json={"titel": "arbeitsordner %s" % ordner, "confirm": True})
            assert r.status_code == code and _n_create() == n0 + (code == 200), (ordner, r.status_code, r.text)
            if code == 200:
                assert r.json()["status"] == "started" and ("--workspace %s --max-retries" % workspace) in _aufrufe().splitlines()[-3], (ordner, _aufrufe()[-400:])
            else:
                assert "not inside a folder marked writable" in r.json()["detail"], r.text
        # switch ON: a card in a read-only working folder can be created (blocked), but Release refuses it and hermes is not called
        os.environ["TROVE_FREIGABE_PFLICHT"] = "1"
        os.environ["TROVE_ARBEITSORDNER"] = inbox
        r = http.post(U + "/auftrag", json={"titel": "an, nur lesbar", "confirm": True})
        k = r.json()
        assert r.status_code == 200 and k["status"] == "blocked" and "--workspace dir:%s " % os.path.realpath(inbox) in _aufrufe().splitlines()[-1], (r.text, _aufrufe()[-300:])
        a0 = _aufrufe()
        r = http.post(U + "/auftrag/%s/freigeben" % k["id"], json=OK)
        assert r.status_code == 403 and "not inside a folder marked writable" in r.json()["detail"], (r.status_code, r.text)
        assert _aufrufe() == a0 and _karten(mod)[k["id"]]["status"] == "blocked", "no unblock for a card in a read-only working folder"
        assert _karten(mod)[k["id"]]["arbeitsordner"] == os.path.realpath(inbox), _karten(mod)[k["id"]]
        # Release checks the folder stored with the card (its --workspace), not today's variable: (a) variable now points to a
        # writable folder -> still 403
        os.environ["TROVE_ARBEITSORDNER"] = archiv
        r = http.post(U + "/auftrag/%s/freigeben" % k["id"], json=OK)
        assert r.status_code == 403 and _aufrufe() == a0, (r.status_code, r.text)
        # (c) variable removed after the card was created -> 403 (the stored folder is checked, not "no variable")
        os.environ["TROVE_ARBEITSORDNER"] = inbox
        kc = http.post(U + "/auftrag", json={"titel": "an, variable weg", "confirm": True}).json()
        os.environ.pop("TROVE_ARBEITSORDNER")
        a0 = _aufrufe()
        r = http.post(U + "/auftrag/%s/freigeben" % kc["id"], json=OK)
        assert r.status_code == 403 and _aufrufe() == a0, (r.status_code, r.text)
        # (b) created through the link zeiger -> archive/arbeit: the card holds the resolved archive/arbeit. Afterwards zeiger is
        # re-pointed and archive/arbeit itself becomes a link into the read-only inbox: Release resolves again -> 403
        os.environ["TROVE_ARBEITSORDNER"] = zeiger
        kb = http.post(U + "/auftrag", json={"titel": "an, link", "confirm": True}).json()
        assert _karten(mod)[kb["id"]]["arbeitsordner"] == os.path.realpath(os.path.join(archiv, "arbeit")), _karten(mod)[kb["id"]]
        os.remove(zeiger)
        os.symlink(inbox, zeiger)
        os.rename(os.path.join(archiv, "arbeit"), os.path.join(archiv, "arbeit.alt"))
        os.symlink(inbox, os.path.join(archiv, "arbeit"))
        a0 = _aufrufe()
        r = http.post(U + "/auftrag/%s/freigeben" % kb["id"], json=OK)
        assert r.status_code == 403 and _aufrufe() == a0, (r.status_code, r.text)
        os.remove(os.path.join(archiv, "arbeit"))
        os.rename(os.path.join(archiv, "arbeit.alt"), os.path.join(archiv, "arbeit"))
        # the human marks Inbox writable: the stored folder passes now -> 200 + unblock
        iid = quellen["Inbox"]["id"]
        http.post(U + "/quellen/" + iid + "/schreibbar", json={"nur_lesen": False})
        try:
            assert http.post(U + "/auftrag/%s/freigeben" % k["id"], json=OK).status_code == 200 and "kanban unblock %s" % k["id"] in _aufrufe()[len(a0):]
        finally:
            http.post(U + "/quellen/" + iid + "/schreibbar", json={})
        for x in (kb, kc):
            http.post(U + "/auftrag/%s/archivieren" % x["id"], json=OK)
        os.environ["TROVE_ARBEITSORDNER"] = inbox
        a0 = _aufrufe()
        r = http.post(U + "/auftrag/t_unbekannt1/freigeben", json=OK)
        assert r.status_code == 403 and _aufrufe() == a0, (r.status_code, r.text)
    finally:
        os.environ.pop("TROVE_ARBEITSORDNER", None)
        os.environ["TROVE_FREIGABE_PFLICHT"] = "1"
        os.remove(weg)
        os.remove(zeiger)
    # ---- MCP in this process (Client(server)), so the wait of the queue lock can be shortened without a test option in the product
    import mcp_server
    srv = mcp_server.bauen(mandant)
    n0, n_queue = _n_create(), len(mq.liste(mandant))
    sperre = mq._pfad(mandant) + ".lock"
    os.environ["TROVE_ARBEITSORDNER"] = inbox
    alt_warten = bp.WarteSperre.warten_s
    try:
        async with Client(srv, client_info=Implementation(name="test-client", version="1")) as c:
            msg, err = await _ruf(c, "request_agent_task", title="im nur lesbaren Ordner", text="x")
            assert err and "not inside a folder marked writable" in msg and _n_create() == n0 and len(mq.liste(mandant)) == n_queue, msg
            # the request queue held by a living process for the whole wait: MCP refuses, nothing queued; the tab's archive -> 503
            wartet = mq.anlegen(mandant, "test-client", "sync", leer, "t", "d")
            n_queue += 1
            belegt, los = threading.Event(), threading.Event()

            def halten():
                with bp.WarteSperre(sperre):
                    belegt.set()
                    los.wait(60)
            t = threading.Thread(target=halten)
            t.start()
            belegt.wait(5)
            bp.WarteSperre.warten_s = 0.3  # instead of 20 s
            try:
                msg, err = await _ruf(c, "request_sync", from_source=q["id"], to_source=z["id"])
                r = http.post(U + "/auftrag/%s/archivieren" % wartet["id"], json=OK)
            finally:
                los.set()
                t.join()
            assert err and "queue is busy, nothing was queued" in msg and len(mq.liste(mandant)) == n_queue, msg
            assert r.status_code == 503 and "busy" in r.json()["detail"] and mq.holen(mandant, wartet["id"])["status"] == "blocked", (r.status_code, r.text)
            assert http.post(U + "/auftrag/%s/archivieren" % wartet["id"], json=OK).status_code == 200
            # switch OFF, the receipt log is held by a long run when the request would start: the client hears "blocked" and
            # "NOT started", and the request is back to blocked (not stuck in running). Same queue module as the server here.
            os.environ["TROVE_FREIGABE_PFLICHT"] = "0"
            echt = mq._log

            def log_belegt(m, kid, meldung, **k):
                if "started directly" in meldung:
                    raise bp.ProtokollGesperrt("busy")
                return echt(m, kid, meldung, **k)
            mq._log = log_belegt
            try:
                r, err = await _ruf(c, "request_sync", from_source=q["id"], to_source=z["id"])
            finally:
                mq._log = echt
                os.environ["TROVE_FREIGABE_PFLICHT"] = "1"
            assert not err and r["status"] == "blocked" and "NOT started" in r["note"], r
            assert mq.holen(mandant, r["task_id"])["status"] == "blocked", mq.holen(mandant, r["task_id"])
            assert http.post(U + "/auftrag/%s/archivieren" % r["task_id"], json=OK).status_code == 200
    finally:
        bp.WarteSperre.warten_s = alt_warten
        os.environ.pop("TROVE_ARBEITSORDNER", None)
    # ---- lock discipline: anlegen holds the queue lock only briefly while it waits for the receipt log, so the end of a running
    # request is saved meanwhile (done, not stuck in running); the end of a run retries while the queue is busy
    pf = bp.pfad_fuer(mandant)
    alt = mq.ANLEGEN_LOG_WARTEN_S, bp.WarteSperre.warten_s, mq.SETZEN_FRIST_S
    mq.ANLEGEN_LOG_WARTEN_S, bp.WarteSperre.warten_s = 0.3, 0.8  # the receipt log is held 1.5 s: longer than both
    try:
        e = mq.anlegen(mandant, "sperr-client", "sync", leer, "t", "d")
        mq._setzen(mandant, e["id"], ("blocked",), status="running", freigegeben_ts=time.time())
        n_queue = len(mq.liste(mandant))
        halter = subprocess.Popen([sys.executable, "-c", "import sys, time; sys.path.insert(0, %r); import belegprotokoll as bp\n"
                                   "with bp.Protokoll(%r):\n    print('held', flush=True)\n    time.sleep(1.5)" % (os.path.dirname(mq.__file__), pf)],
                                  stdout=subprocess.PIPE, text=True)
        assert halter.stdout.readline().strip() == "held"
        neu = {}

        def anlegen_waehrend():
            try:
                neu["e"] = mq.anlegen(mandant, "sperr-client", "sync", leer, "t2", "d")
            except bp.ProtokollGesperrt as ex:
                neu["gesperrt"] = ex
        t = threading.Thread(target=anlegen_waehrend)
        t.start()
        ende = time.time() + 10
        while not _gehalten(sperre):  # anlegen holds the queue lock and waits for the receipt log
            assert time.time() < ende and t.is_alive(), "anlegen never held the queue lock"
            time.sleep(0.005)
        fertig = mq._setzen(mandant, e["id"], None, status="done")  # the end of the running request, written right now
        t.join()
        halter.wait(30)
        assert fertig is not None and mq.holen(mandant, e["id"])["status"] == "done", mq.holen(mandant, e["id"])
        assert "gesperrt" in neu and len(mq.liste(mandant)) == n_queue, neu  # refused, nothing queued

        # the end of a run while the queue is held longer than one wait: retried until saved
        bp.WarteSperre.warten_s = 0.15
        belegt = threading.Event()

        def halten(dauer):
            with bp.WarteSperre(sperre):
                belegt.set()
                time.sleep(dauer)

        class HaltApi:
            def aktion(self, body):  # the run ends while someone holds the queue for 0.5 s (more than three waits of 0.15 s)
                belegt.clear()
                threading.Thread(target=halten, args=(0.5,)).start()
                belegt.wait(5)
                return {"lauf": "x"}
        e = mq.anlegen(mandant, "sperr-client", "sync", leer, "t3", "d")
        mq.freigeben(mandant, e["id"], HaltApi(), warten=True, direkt=True)
        assert mq.holen(mandant, e["id"])["status"] == "done", mq.holen(mandant, e["id"])
        # ... and after SETZEN_FRIST_S it gives up with a log line instead of ending silently
        mq.SETZEN_FRIST_S = 0
        e = mq.anlegen(mandant, "sperr-client", "sync", leer, "t4", "d")
        mq.freigeben(mandant, e["id"], HaltApi(), warten=True, direkt=True)
        assert mq.holen(mandant, e["id"])["status"] == "running" and any("%s from sperr-client: result could not be saved" % e["id"] in z for z in _log_texte(mandant))
        mq.SETZEN_FRIST_S = alt[2]
        mq._setzen_wartend(mandant, e["id"], None, status="failed")  # tidy up once the holder is gone
    finally:
        mq.ANLEGEN_LOG_WARTEN_S, bp.WarteSperre.warten_s, mq.SETZEN_FRIST_S = alt
    # ---- the limits count agent tasks for as long as their agent can run (after-run time), file actions only until they end
    class SofortApi:
        def _auftrag_anlegen(self, body, herkunft, starten):
            return {"id": "t_sofort"}

        def aktion(self, body):
            return {"lauf": "sofort"}

    def nacheinander(client, art, body, n):
        ids = []
        for _ in range(n):
            e = mq.anlegen(mandant, client, art, body, "t", "d")
            mq.freigeben(mandant, e["id"], SofortApi(), warten=True, direkt=True)
            assert mq.holen(mandant, e["id"])["status"] == "done", mq.holen(mandant, e["id"])
            ids.append(e["id"])
        return ids

    agent = {"titel": "flut", "text": "x"}
    ids = nacheinander("agent-client", "agent_auftrag", agent, mq.MAX_OFFEN_JE_CLIENT)  # one after the other, each one done at once
    try:
        mq.anlegen(mandant, "agent-client", "agent_auftrag", agent, "t", "d")
        raise AssertionError("the 21st agent task of one client must be refused while the agents can still run")
    except ValueError as ex:
        assert "an agent task finished less than 30 min ago; archiving does not free it" in str(ex), ex
    mq._setzen(mandant, ids[0], None, belegt_bis=time.time() - 1)  # that agent's time is over: its place is free again
    mq.ablehnen(mandant, mq.anlegen(mandant, "agent-client", "agent_auftrag", agent, "t", "d")["id"])
    nacheinander("datei-client", "sync", leer, mq.MAX_OFFEN_JE_CLIENT + 1)  # a finished file action does not count
    # global cap with running requests of several clients
    i = 0
    while sum(1 for x in mq.liste(mandant) if mq._offen(x, time.time())) < mq.MAX_OFFEN_GESAMT:
        e = mq.anlegen(mandant, "gesamt-%d" % (i % 3), "sync", leer, "t", "d")
        mq._setzen(mandant, e["id"], ("blocked",), status="running", freigegeben_ts=time.time())
        i += 1
    assert i >= 3, i
    try:
        mq.anlegen(mandant, "gesamt-3", "sync", leer, "t", "d")
        raise AssertionError("the global cap must refuse the 51st open request")
    except ValueError as ex:
        assert "requests are still open" in str(ex), ex
    for x in mq.liste(mandant):
        if x["client"].startswith("gesamt-") and x["status"] == "running":
            mq._setzen(mandant, x["id"], None, status="done")
    texte = " | ".join(_log_texte(mandant, "trove-mcp"))
    assert "started directly (freigabe_pflicht off)" in texte and "queued: " in texte, texte
    print("aus ok")


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
        os.environ["TROVE_HERMES"] = _fake_hermes(tmp)
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
        os.environ["TROVE_FREIGABE_PFLICHT"] = "1"  # the long section below tests the switch-ON path: every change waits for Release
        env = dict(os.environ, PYTHONPATH="")
        server_py = os.path.join(dash, "mcp_server.py")
        asyncio.run(stdio_teil(server_py, env, http, mod, tmp))
        asyncio.run(aus_teil(server_py, env, http, mod, tmp))
        web_teil(http, mod)
        rennen_teil(http, mod)
        os.environ["TROVE_FREIGABE_PFLICHT"] = "0"
        asyncio.run(http_teil(server_py, dict(env, TROVE_FREIGABE_PFLICHT="0")))
        import belegprotokoll as bp
        pr = bp.pruefen(bp.pfad_fuer(mod.MANDANT))
        assert pr.get("ok", True) is not False, pr  # the receipt-log chain is intact after all sections
        # K6: without the mcp package -> one clear line, no traceback
        r = subprocess.run([sys.executable, "-c", "import sys, runpy; sys.modules['mcp'] = None; sys.argv = ['mcp_server.py']; "
                            "runpy.run_path(%r, run_name='__main__')" % server_py], capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode != 0 and "pip install mcp" in r.stderr and "Traceback" not in r.stderr, r.stderr
        print("mcp ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
