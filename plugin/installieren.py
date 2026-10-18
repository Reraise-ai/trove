"""Trove-Plugin in Hermes installieren (Kopie aus dem Repo nach %HERMES_HOME%\\plugins\\trove, Eintrag in config.yaml plugins.enabled).
Idempotent. Quelle der Wahrheit bleibt C:\\Projects\\trove\\plugin. Aufruf: python plugin\\installieren.py
"""
import os, shutil, sys, time, json
HOME = os.environ.get("HERMES_HOME") or os.path.join(os.environ["LOCALAPPDATA"], "hermes")
QUELLE = os.path.dirname(os.path.abspath(__file__))
ZIEL = os.path.join(HOME, "plugins", "trove")
DATEIEN = ["plugin.yaml", "__init__.py", "dashboard/manifest.json", "dashboard/plugin_api.py"]
for rel in DATEIEN:
    q = os.path.join(QUELLE, *rel.split("/")); z = os.path.join(ZIEL, *rel.split("/"))
    os.makedirs(os.path.dirname(z), exist_ok=True); shutil.copyfile(q, z)
# Desktop-Haelfte als REINES Disk-Plugin in den App-Root (Hot-Load, standardmaessig AN).
# KEIN .hermes-package.json: mit Marker gilt in der App "opt-in" (defaultEnabled=false, runtime-loader.ts),
# dann bleibt der Tab aus, bis jemand ihn unter Capabilities -> Plugins einschaltet (Befund 18.10. 02:55).
# Deshalb liegt desktop/plugin.js NICHT unter plugins\trove (sonst materialisiert die App eine Marker-Kopie).
DROOT = os.path.join(HOME, "desktop-plugins", "trove")
os.makedirs(DROOT, exist_ok=True)
shutil.copyfile(os.path.join(QUELLE, "desktop", "plugin.js"), os.path.join(DROOT, "plugin.js"))
for alt in (os.path.join(DROOT, ".hermes-package.json"), os.path.join(ZIEL, "desktop", "plugin.js")):
    if os.path.exists(alt):
        os.remove(alt)
try:
    os.rmdir(os.path.join(ZIEL, "desktop"))
except OSError:
    pass
cfg = os.path.join(HOME, "config.yaml")
txt = open(cfg, encoding="utf-8").read()
geaendert = False
if "\n  - trove\n" not in txt and "- trove" not in txt:
    import yaml
    c = yaml.safe_load(txt)
    en = c.setdefault("plugins", {}).setdefault("enabled", [])
    if "trove" not in en:
        bak = cfg + ".bak-vor-trove-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copyfile(cfg, bak)
        # minimal-invasiv: nur die Zeile unter 'enabled:' ergaenzen, Rest der Datei byteidentisch
        zeilen = txt.splitlines(keepends=True); out = []; i = 0; eingefuegt = False
        while i < len(zeilen):
            out.append(zeilen[i])
            if not eingefuegt and zeilen[i].strip() == "enabled:":
                j = i + 1
                while j < len(zeilen) and zeilen[j].lstrip().startswith("- "):
                    out.append(zeilen[j]); j += 1
                einzug = zeilen[i + 1][:len(zeilen[i + 1]) - len(zeilen[i + 1].lstrip())] if j > i + 1 else "    "
                out.append(einzug + "- trove\n"); eingefuegt = True; i = j; continue
            i += 1
        if eingefuegt:
            open(cfg, "w", encoding="utf-8", newline="").write("".join(out)); geaendert = True
            print("config.yaml: plugins.enabled += trove (Sicherung %s)" % bak)
        else:
            print("WARNUNG: 'enabled:' nicht gefunden – config.yaml unveraendert"); sys.exit(2)
print(json.dumps({"ziel": ZIEL, "desktop_root": DROOT, "dateien": DATEIEN, "config_geaendert": geaendert}))
