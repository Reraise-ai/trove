#!/usr/bin/env python3
"""End-to-end test of "Into knowledge" (wissen.py) with the default knowledge target (a wiki folder).

Runs wissen.py in a subprocess against a temp data root: no TROVE_WIKI set -> the default wiki is created under the data root,
twin + card + log line are written, a second run changes nothing, a duplicate is reported, the original is untouched, and the missing
qmd tool is reported as skipped (not as an error). Then the same with an explicit, existing TROVE_WIKI.
"""
import glob
import hashlib
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WISSEN = os.path.join(REPO, "plugin", "dashboard", "wissen.py")


def lauf(env, quelle, *extra):
    e = dict(os.environ, **env)
    for k in ("TROVE_WIKI", "TROVE_BRETT", "TROVE_KARTENZIEL"):
        e.pop(k, None) if k not in env else None
    r = subprocess.run([sys.executable, WISSEN, quelle, "--sammlung", "t1", "--karten", "--ohne-embed", "--json", *extra],
                       capture_output=True, text=True, env=e, timeout=600)
    assert r.returncode == 0, (r.stdout + r.stderr)[-1500:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def pruefe(tmp, env, wiki):
    quelle = os.path.join(tmp, "inbox")
    os.makedirs(quelle)
    inhalt = "Invoice 4711 from ACME Ltd. Total 120.00 EUR. Thank you for your business, this is a plain text document.\n"
    for n in ("a.txt", "b-copy.txt"):
        with open(os.path.join(quelle, n), "w", encoding="utf-8") as f:
            f.write(inhalt)
    with open(os.path.join(quelle, "note.md"), "w", encoding="utf-8") as f:
        f.write("# Meeting\nWe decided to ship Trove with English UI first.\n")
    vorher = {n: hashlib.sha256(open(os.path.join(quelle, n), "rb").read()).hexdigest() for n in os.listdir(quelle)}
    z = lauf(env, quelle)
    assert z["fehler"] == 0 and z["neu"] == 2 and z["dubletten"] == 1, z
    assert z["karten"] == 2, z
    assert os.path.isdir(wiki), wiki
    karten = glob.glob(os.path.join(wiki, "quellen", "trove-*.md"))
    assert len(karten) == 2, karten
    assert "status: to-review" in open(karten[0], encoding="utf-8").read()
    twins = glob.glob(os.path.join(os.path.dirname(wiki), "raw", "trove", "t1", "*.md"))
    assert len(twins) == 2, twins
    log = open(os.path.join(wiki, "log.md"), encoding="utf-8").read()
    assert "Trove" in log, log
    qmd = z.get("qmd") or {}
    assert not qmd.get("fehler"), qmd  # missing qmd is "skipped", never an error
    z2 = lauf(env, quelle)
    assert z2["neu"] == 0 and z2["fehler"] == 0 and len(glob.glob(os.path.join(wiki, "quellen", "trove-*.md"))) == 2, z2
    nachher = {n: hashlib.sha256(open(os.path.join(quelle, n), "rb").read()).hexdigest() for n in os.listdir(quelle)}
    assert vorher == nachher, "originals changed"
    # the wiki and the data root are never read back in as a source (Trove does not read its own cards)
    r = subprocess.run([sys.executable, WISSEN, wiki, "--sammlung", "t2", "--json"], capture_output=True, text=True, env=dict(os.environ, **env))
    assert r.returncode != 0 or json.loads(r.stdout.strip().splitlines()[-1]).get("dateien", 0) == 0, r.stdout[-500:]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        daten = os.path.join(tmp, "daten")
        env = {"TROVE_DATEN": daten, "TROVE_MANDANT": "test", "TROVE_QMD": "/nonexistent/qmd", "PATH": "/usr/bin:/bin"}
        pruefe(tmp, env, os.path.join(daten, "knowledge", "wiki"))
    with tempfile.TemporaryDirectory() as tmp:
        wiki = os.path.join(tmp, "Wissen", "wiki")
        os.makedirs(wiki)
        env = {"TROVE_DATEN": os.path.join(tmp, "daten"), "TROVE_MANDANT": "test", "TROVE_WIKI": wiki, "TROVE_QMD": "/nonexistent/qmd", "PATH": "/usr/bin:/bin"}
        pruefe(tmp, env, wiki)
    print("test_wissen ok")


if __name__ == "__main__":
    main()
