#!/usr/bin/env python3
"""End-to-end test of "Into knowledge" (wissen.py) with the default knowledge target (a wiki folder).

Runs wissen.py in a subprocess against a temp data root: no TROVE_WIKI set -> the default wiki is created under the data root,
twin + card + log line are written, a second run changes nothing, a duplicate is reported, the original is untouched, and the missing
qmd tool is reported as skipped (not as an error). Then the same with an explicit, existing TROVE_WIKI.
Then the setting karten_befehl with a fake command: cards go to the command (target from --ziel, default belege), not to the wiki;
the twin gets karte: <id> and the receipt log one line per card; a second run calls the command no more; exit code != 0 = no card.
The card's head is valid YAML: names with ": ", " #" and quotes come back exactly (gm.kopf_lesen and, if installed, yaml.safe_load).
"""
import glob
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WISSEN = os.path.join(REPO, "plugin", "dashboard", "wissen.py")
sys.path.insert(0, os.path.dirname(WISSEN))
import gemeinsam as gm  # noqa: E402


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


FAKE_BEFEHL = r"""
import json, os, sys
a = sys.argv[1:]
ziel, datei, log = a[a.index("--ziel") + 1], a[a.index("--datei") + 1], os.environ["FAKE_LOG"]
text = open(datei, encoding="utf-8").read()
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps({"ziel": ziel, "karte": text}) + "\n")
if os.path.exists(log + ".fehler"):
    sys.stderr.write("board refused the card")
    sys.exit(3)
n = sum(1 for _ in open(log, encoding="utf-8"))
print("some chatter first")
print(json.dumps({"id": "k-%d" % n, "pfad": "/board/%s/k-%d.md" % (ziel, n)}))
"""


def karten_befehl(tmp):
    daten, quelle, log = os.path.join(tmp, "daten"), os.path.join(tmp, "inbox"), os.path.join(tmp, "befehl.log")
    os.makedirs(quelle)
    os.makedirs(daten)
    for n, inhalt in (("a.txt", "Invoice 4711 from ACME Ltd. Total 120.00 EUR."), ("b.txt", "Meeting note: ship Trove in English first.")):
        with open(os.path.join(quelle, n), "w", encoding="utf-8") as f:
            f.write(inhalt)
    fake = os.path.join(tmp, "fake befehl.py")  # a space in the path: the setting is split like a shell would
    with open(fake, "w", encoding="utf-8") as f:
        f.write(FAKE_BEFEHL)
    with open(os.path.join(daten, "trove.json"), "w", encoding="utf-8") as f:
        json.dump({"karten_befehl": "%s %s" % (shlex.quote(sys.executable), shlex.quote(fake))}, f)
    env = {"TROVE_DATEN": daten, "TROVE_MANDANT": "test", "TROVE_QMD": "/nonexistent/qmd", "PATH": "/usr/bin:/bin", "FAKE_LOG": log}
    aufrufe = lambda: [json.loads(z) for z in open(log, encoding="utf-8")] if os.path.exists(log) else []  # noqa: E731
    wiki = os.path.join(daten, "knowledge", "wiki")

    def zwillinge(sammlung):
        return {os.path.basename(p): open(p, encoding="utf-8").read() for p in glob.glob(os.path.join(wiki, "..", "raw", "trove", sammlung, "*.md"))}

    # exit code != 0: no card, the twin gets no karte: line, the run reports the card errors
    open(log + ".fehler", "w").close()
    z = subprocess.run([sys.executable, WISSEN, quelle, "--sammlung", "t1", "--karten", "--ohne-embed", "--json"], capture_output=True, text=True,
                       env=dict(os.environ, **env), timeout=600)
    z = json.loads(z.stdout.strip().splitlines()[-1])
    assert z["karten"] == 0 and z["fehler_karte"] == 2 and len(aufrufe()) == 2, z
    assert "board refused the card" in z["eintraege"][0]["fehler"], z["eintraege"][0]
    assert all("\nkarte:" not in t for t in zwillinge("t1").values()), zwillinge("t1")
    os.remove(log + ".fehler")
    # success, target from --ziel (as over MCP): every card goes to the command, nothing into the wiki
    z = lauf(env, quelle, "--ziel", "akteure")
    assert z["karten"] == 2 and z["fehler"] == 0, z
    neu = aufrufe()[2:]
    assert [x["ziel"] for x in neu] == ["akteure", "akteure"], neu
    karte = neu[0]["karte"]
    assert karte.startswith("---\ntyp: \"quelle\"\n") and gm.kopf_lesen(karte)["sichtbarkeit"] == "privat", karte
    assert gm.kopf_lesen(karte)["pruefsumme"] and gm.kopf_lesen(karte)["titel"], karte
    assert not glob.glob(os.path.join(wiki, "quellen", "*.md")), "no wiki card with karten_befehl"
    ids = sorted(t.split("\nkarte: ")[1].split("\n")[0].strip('"') for t in zwillinge("t1").values())
    assert ids == ["k-3", "k-4"], ids
    zeilen = [json.loads(x) for x in open(os.path.join(daten, "test", "belegprotokoll.jsonl"), encoding="utf-8")]
    assert sorted(x["karte"] for x in zeilen if x.get("karte") and x.get("karten_ziel") == "akteure") == ["k-3", "k-4"], zeilen[-6:]
    # again: the head line karte: counts, the command is not called again, no second card
    z = lauf(env, quelle, "--ziel", "akteure")
    assert z["karten"] == 0 and z["fehler"] == 0 and len(aufrufe()) == 4, (z, len(aufrufe()))
    # without --ziel (the tab): target belege
    os.makedirs(os.path.join(quelle, "neu"))
    with open(os.path.join(quelle, "neu", "c.txt"), "w", encoding="utf-8") as f:
        f.write("A third document with its own text.")
    z = lauf(env, os.path.join(quelle, "neu"))
    assert z["karten"] == 1 and aufrufe()[-1]["ziel"] == "belege", (z, aufrufe()[-1]["ziel"])
    # names a raw "key: value" head would break (": ", " #", quotes): titel and source come back exactly
    sonder = os.path.join(quelle, "sonder")
    namen = ("Rechnung: März.txt", "Rechnung #42.txt", '"zitiert" am Ende "x".txt')
    os.makedirs(sonder)
    for i, n in enumerate(namen):
        with open(os.path.join(sonder, n), "w", encoding="utf-8") as f:
            f.write("Plain note number %s without amounts." % "abc"[i])
    z = lauf(env, sonder)
    assert z["karten"] == 3 and z["fehler"] == 0, z
    karten = [x["karte"] for x in aufrufe()[-3:]]
    import wissenskarte as wk  # a title that starts and ends with a quote (a file name always ends with its extension)
    alt = dict(os.environ)
    os.environ.update(env)  # karte_ablegen in this process: the same fake command and log
    try:
        wk.karte_ablegen(json.load(open(os.path.join(daten, "trove.json")))["karten_befehl"], "belege",
                         {"titel": '"zitiert" am Ende "x"', "source": "/in/a #1: b", "quelle_datum": None}, "# x\n")
    finally:
        os.environ.clear()
        os.environ.update(alt)
    erwartet = {n: os.path.join(sonder, n) for n in namen}
    erwartet['"zitiert" am Ende "x"'] = "/in/a #1: b"
    gesehen = set()
    for karte in karten + [aufrufe()[-1]["karte"]]:
        lesarten = [gm.kopf_lesen(karte)]
        try:
            import yaml
            lesarten.append(yaml.safe_load(karte.split("---\n")[1]))
        except ImportError:
            pass
        titel = lesarten[0]["titel"]
        gesehen.add(titel)
        source = erwartet.get(titel)
        for kopf in lesarten:
            assert kopf["titel"] == titel and kopf["source"] == source, (titel, kopf)
    assert gesehen == set(erwartet), gesehen


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
    with tempfile.TemporaryDirectory() as tmp:
        karten_befehl(tmp)
    print("test_wissen ok")


if __name__ == "__main__":
    main()
