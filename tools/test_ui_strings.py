#!/usr/bin/env python3
"""UI language checks for the Hermes Desktop plugin (static, no browser needed).

1. English is the default: outside the German bundle (<i18n:de> block) plugin.js contains no German words or umlauts.
2. Every key the code uses (t('key')) exists in the English AND the German bundle; both bundles have the same keys; no key is unused.
3. User-facing backend messages (HTTPException / error helpers / ValueError / sys.exit / print) contain no German.
4. The plugin id in plugin.js matches the dashboard manifest name; no deprecated host bridges are used.
Optional: runs tools/ui_smoke.mjs (needs node + react) when TROVE_UI_NODE_MODULES points at a node_modules directory."""
import json
import os
import re
import shutil
import subprocess
import sys

HIER = os.path.dirname(os.path.abspath(__file__))
WURZEL = os.path.dirname(HIER)
JS = os.path.join(WURZEL, "plugin", "desktop", "plugin.js")
DASH = os.path.join(WURZEL, "plugin", "dashboard")
fehler = []


def pruefe(ok, msg):
    print(("ok   " if ok else "FAIL ") + msg)
    if not ok:
        fehler.append(msg)


DEUTSCH = re.compile(r"[äöüÄÖÜß]|\b(nicht|und|oder|der|die|das|dem|den|ist|sind|wird|werden|fehlt|keine?|mit|für|bitte|noch|kann|auch|dann|wenn|aber|zum|zur|nur|Datei|Dateien|Ordner|Fehler|Quelle|erst|gleich|bereits|vorhanden|oeffentlich)\b")
# words that are legitimately German-spelled machine values / identifiers used in English text
ERLAUBT = re.compile(r"oeffentlich|nur_lesen|offen|Dauerlauf|installieren\.py|\bdie\b(?= in the)")


def zeilen_mit_deutsch(text):
    treffer = []
    for i, z in enumerate(text.split("\n"), 1):
        z2 = ERLAUBT.sub("", z)
        z2 = re.sub(r"\b(?:ctx|host)\.\w+", "", z2)
        if DEUTSCH.search(z2):
            treffer.append((i, z.strip()[:140]))
    return treffer


js = open(JS, encoding="utf-8").read()
m_en = re.search(r"// <i18n:en>\n(.*?)// </i18n:en>", js, re.S)
m_de = re.search(r"// <i18n:de>\n(.*?)// </i18n:de>", js, re.S)
pruefe(bool(m_en and m_de), "plugin.js has marked en and de bundles")
ohne_de = js.replace(m_de.group(0), "")
t = zeilen_mit_deutsch(ohne_de)
for i, z in t[:15]:
    print("      German outside the de bundle, line %d: %s" % (i, z))
pruefe(not t, "no German outside the de bundle of plugin.js")

SCHLUESSEL = re.compile(r"(?:^[ \t]+|,[ \t]+)([A-Za-z_][A-Za-z0-9_]*): (?=['(])", re.M)
ken = SCHLUESSEL.findall(m_en.group(1))
kde = SCHLUESSEL.findall(m_de.group(1))
pruefe(len(ken) == len(set(ken)) and len(kde) == len(set(kde)), "no duplicate keys inside a bundle")
pruefe(set(ken) == set(kde), "en and de have the same keys" + ("" if set(ken) == set(kde) else " (diff: %s)" % sorted(set(ken) ^ set(kde))))
verwendet = set(re.findall(r"\b[tT]\('([A-Za-z_][A-Za-z0-9_]*)'\s*[,)]", ohne_de))
dynamisch = {"sort": ["Name", "Size", "Date", "Type"], "act_": ["kopieren", "verschieben", "sync", "lesen", "stapel", "spur"], "art_": ["lesen", "stapel"],
             "trace_": ["direkt", "rueckwaerts", "vorwaerts", "gleicher_inhalt"]}
for pre, namen in dynamisch.items():
    verwendet |= {pre + n for n in namen}
pruefe(verwendet <= set(ken), "every key used in code exists in en" + ("" if verwendet <= set(ken) else " (missing: %s)" % sorted(verwendet - set(ken))))
pruefe(set(ken) <= verwendet, "no unused keys" + ("" if set(ken) <= verwendet else " (unused: %s)" % sorted(set(ken) - verwendet)))

# backend messages
ausgaben = re.compile(r"(HTTPException\(|_fehler\(|raise ValueError\(|sys\.exit\(|print\()")
bad = []
for f in sorted(os.listdir(DASH)):
    if not f.endswith(".py"):
        continue
    for i, z in enumerate(open(os.path.join(DASH, f), encoding="utf-8").read().split("\n"), 1):
        if ausgaben.search(z) and not z.lstrip().startswith("#"):
            lit = " ".join(re.findall(r'"([^"]*)"|\'([^\']*)\'', z) and [a or b for a, b in re.findall(r'"([^"]*)"|\'([^\']*)\'', z)])
            if zeilen_mit_deutsch(lit):
                bad.append("%s:%d %s" % (f, i, z.strip()[:120]))
for b in bad[:20]:
    print("      " + b)
pruefe(not bad, "no German in user-facing backend messages (%d found)" % len(bad))

# id / bridges
man = json.load(open(os.path.join(DASH, "manifest.json"), encoding="utf-8"))
mid = re.search(r"const ID = '([^']+)'", js).group(1)
pruefe(man.get("name") == mid, "plugin.js ID (%s) matches manifest name (%s)" % (mid, man.get("name")))
pruefe("hermesDesktop" not in js and "selectPaths" not in js and "recycleBackend" not in js, "no undocumented host bridges (window.hermesDesktop, selectPaths, recycleBackend)")
ohne_diktat = re.sub(r"^const DICTATE_LANG = 'de-DE'.*$", "", js, flags=re.M)  # the one allowed use: the dictation language (spec: always German)
pruefe(len(re.findall(r"^const DICTATE_LANG = 'de-DE'", js, flags=re.M)) == 1 and ohne_diktat.count("DICTATE_LANG") == 1 and "de-DE" not in ohne_diktat and "'de'" not in ohne_de,
       "no hard-coded locale for dates (de-DE only in the DICTATE_LANG constant, used once for speech recognition)")

# the folder rule (a warning, not a lock) names trove.json and Hermes' folder where a foreign AI reads it, so it knows why a source carries a
# warnung. It also says that a second mount is not detected, and never promises "another path to the same folder" again.
for rel in ("llms.txt", "skills/trove/SKILL.md"):
    regel = [z for z in open(os.path.join(WURZEL, rel), encoding="utf-8") if "mark" in z and "writable" in z and "own data folder" in z]
    pruefe(any("`trove.json`" in z and "second mount" in z for z in regel), rel + ": folder rule names trove.json and warns about a second mount")
    pruefe(any("TROVE_PYTHON" in z for z in regel), rel + ": folder rule says the venv (TROVE_PYTHON) and user site are not protected")
    pruefe(any("Hermes' folder, or of a folder inside them" in z for z in regel), rel + ": second-mount warning covers the install folder and Hermes' folder")
for rel, warnung in (("README.md", "second mount"), ("README.de.md", "zweites Mal")):
    pruefe(warnung in open(os.path.join(WURZEL, rel), encoding="utf-8").read(), rel + ": warns about a second mount of the data root")
# the MCP process protects only the trove.json and Hermes' folder of its own environment
for rel, satz in (("llms.txt", "`TROVE_DATEN` and `HERMES_HOME`"), ("README.md", "`TROVE_DATEN` and `HERMES_HOME`"), ("README.de.md", "`TROVE_DATEN` und `HERMES_HOME`")):
    pruefe(satz in open(os.path.join(WURZEL, rel), encoding="utf-8").read(), rel + ": MCP server needs the same HERMES_HOME as Hermes")
for rel in ("llms.txt", "skills/trove/SKILL.md", "README.md", "README.de.md", "CHANGELOG.md"):
    text = " ".join(open(os.path.join(WURZEL, rel), encoding="utf-8").read().split())
    pruefe("another path to the same folder" not in text, rel + ": no promise of 'another path to the same folder' (a second mount is not detected)")
    # the venv and user site are not protected: never promise the "whole install"
    pruefe("whole install" not in text and "ganze Installation" not in text and "`TROVE_PYTHON` (Mac" in text, rel + ": no 'whole install' promise, names TROVE_PYTHON")
    pruefe("Hermes' folder `HERMES_HOME`" in text or "Hermes' Ordner `HERMES_HOME`" in text, rel + ": folder rule names HERMES_HOME")
pruefe("anderen Pfad zum selben Ordner" not in " ".join(open(os.path.join(WURZEL, "README.de.md"), encoding="utf-8").read().split()),
       "README.de.md: no promise of 'anderen Pfad zum selben Ordner' (a second mount is not detected)")

# one version everywhere: manifests, backend, READMEs, llms.txt and the latest CHANGELOG release
def _lies(rel):
    return open(os.path.join(WURZEL, rel), encoding="utf-8").read()


def _treffer(rel, muster):
    return re.findall(muster, _lies(rel), re.M)


versionen = {
    "plugin/plugin.yaml": _treffer("plugin/plugin.yaml", r'^version: "([^"]+)"'),
    ".claude-plugin/plugin.json": [json.loads(_lies(".claude-plugin/plugin.json")).get("version")],
    ".claude-plugin/marketplace.json": [x["version"] for x in json.loads(_lies(".claude-plugin/marketplace.json"))["plugins"] if "version" in x],
    "plugin/dashboard/manifest.json": [man.get("version")],
    "plugin/dashboard/plugin_api.py": _treffer("plugin/dashboard/plugin_api.py", r'^VERSION = "([^"]+)"'),
    "README.md": _treffer("README.md", r"Maturity: (\d+\.\d+\.\d+)"),
    "README.de.md": _treffer("README.de.md", r"Reifegrad: (\d+\.\d+\.\d+)"),
    "llms.txt": _treffer("llms.txt", r"Version (\d+\.\d+\.\d+)") + _treffer("llms.txt", r"^(\d+\.\d+\.\d+), "),
    "CHANGELOG.md": _treffer("CHANGELOG.md", r"^## \[(\d+\.\d+\.\d+)\]")[:1],
}
alle = {v for liste in versionen.values() for v in liste}
leer = sorted(k for k, liste in versionen.items() if not liste and k != ".claude-plugin/marketplace.json")
pruefe(len(alle) == 1 and not leer, "one version everywhere (%s)" % ("; ".join("%s=%s" % (k, ",".join(map(str, v)) or "missing") for k, v in versionen.items())))

# optional render smoke test
nm = os.environ.get("TROVE_UI_NODE_MODULES", "")
if nm and shutil.which("node"):
    r = subprocess.run(["node", os.path.join(HIER, "ui_smoke.mjs"), nm], capture_output=True, text=True)
    print(r.stdout[-1500:], r.stderr[-500:])
    pruefe(r.returncode == 0, "UI render smoke test (node + react)")
else:
    print("skip UI render smoke test (set TROVE_UI_NODE_MODULES to a node_modules with react, react-dom, react-test-renderer)")

print("ALL PASSED" if not fehler else "FAILED: %d" % len(fehler))
sys.exit(1 if fehler else 0)
