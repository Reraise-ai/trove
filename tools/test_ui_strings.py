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
pruefe("de-DE" not in js and "'de'" not in ohne_de, "no hard-coded locale for dates")

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
