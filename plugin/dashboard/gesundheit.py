"""Trove health check: Python and packages, data root, qmd (optional search index), wiki folder (knowledge target), hermes CLI (optional,
for agent tasks), Stirling-PDF (optional), continuous run (scheduler, watched folders, last run). Installs nothing, only names the command.
Run: python gesundheit.py. Also GET /api/plugins/trove/gesundheit."""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

HIER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HIER)
import konfig  # noqa: E402

PAKETE = (("pypdfium2", "pypdfium2"), ("pikepdf", "pikepdf"), ("numpy", "numpy"), ("PIL", "pillow"))
OCR = (("onnxruntime", "onnxruntime"), ("rapidocr_onnxruntime", "rapidocr-onnxruntime"))  # fehlt -> nur Hinweis "Scans ohne Texterkennung"
SCHLECHT = ("fehlt", "offen")  # GESAMT "in Ordnung" nur ohne diese


def qmd_ordner():
    """Wo qmd auf dem Mac/Linux typischerweise liegt (auch wenn der Ordner im PATH des Hermes-Fensters/LaunchAgents fehlt)."""
    h = os.path.expanduser("~")
    return [os.path.join(h, ".local", "bin"), "/opt/homebrew/bin", "/usr/local/bin", os.path.join(h, ".bun", "bin")]


def qmd_finden():
    """qmd-Programm: TROVE_QMD (wenn die Datei existiert), Windows %LOCALAPPDATA%\\hermes\\node\\qmd.cmd, PATH, ~/.local/bin,
    /opt/homebrew/bin, /usr/local/bin, ~/.bun/bin. None = nicht gefunden."""
    kand = [os.environ.get("TROVE_QMD")]
    if sys.platform == "win32":
        kand.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes", "node", "qmd.cmd"))
    kand += [shutil.which("qmd")] + [os.path.join(d, "qmd") for d in qmd_ordner()]
    return next((os.path.abspath(p) for p in kand if p and os.path.isfile(p)), None)


def such_pfad(*programme):
    """PATH fuer Unterprozesse und den LaunchAgent: Ordner der gefundenen Programme (qmd, node) + qmd_ordner + Systemordner, ohne Doppelte."""
    ordner = [os.path.dirname(p) for p in programme if p]
    node = shutil.which("node") or next((os.path.join(d, "node") for d in qmd_ordner() if os.path.isfile(os.path.join(d, "node"))), None)
    if node:
        ordner.append(os.path.dirname(node))  # qmd startet per "#!/usr/bin/env node"
    ordner += qmd_ordner() + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(dict.fromkeys(ordner))


def hermes_profile():
    p = os.path.join(konfig.hermes_home(), "profiles")
    namen = {n for n in os.listdir(p) if not n.startswith(".") and os.path.isdir(os.path.join(p, n))} if os.path.isdir(p) else set()
    return sorted(namen | {"default"})


def laufzeit_module(plugin_api, repo):
    """Alle *.py im Repo-Wurzelordner, die plugin_api.py (auch lazy) importiert oder per _werkzeug("x.py") startet — samt Importkette."""
    vorhanden = {n[:-3] for n in os.listdir(repo) if n.endswith(".py")}
    quelle = open(plugin_api, encoding="utf-8").read()
    todo = [m for m in re.findall(r'_werkzeug\(\s*"(\w+)\.py"', quelle)] + ["konfig", "dauerlauf"]
    gesehen = set()
    todo += _importe(quelle, vorhanden)
    while todo:
        n = todo.pop()
        if n in gesehen or n not in vorhanden:
            continue
        gesehen.add(n)
        todo += _importe(open(os.path.join(repo, n + ".py"), encoding="utf-8").read(), vorhanden)
    return sorted(gesehen)


def _importe(quelle, vorhanden):
    out = []
    for k in ast.walk(ast.parse(quelle)):
        if isinstance(k, ast.Import):
            out += [a.name.split(".")[0] for a in k.names]
        elif isinstance(k, ast.ImportFrom) and k.module and k.level == 0:
            out.append(k.module.split(".")[0])
    return [n for n in out if n in vorhanden]


def _plugin_api():
    p = os.path.join(HIER, "plugin_api.py")
    return p if os.path.isfile(p) else None


def _punkt(name, status, detail, hilfe=""):
    return {"name": name, "status": status, "detail": detail, "hilfe": hilfe}






def _qmd_lauf(qmd, *args, timeout=60):
    """qmd so starten wie der Dauerlauf (PATH des LaunchAgents, XDG wie wissen.py). Liefert (rc, ausgabe)."""
    env = dict(os.environ, PATH=such_pfad(qmd) + os.pathsep + os.environ.get("PATH", "") if sys.platform != "win32" else os.environ.get("PATH", ""))
    env["XDG_CACHE_HOME"] = os.environ.get("TROVE_QMD_CACHE", os.path.join(os.path.expanduser("~"), ".cache"))
    env["XDG_CONFIG_HOME"] = os.environ.get("TROVE_QMD_CONFIG", os.path.join(os.path.expanduser("~"), ".config"))
    try:
        r = subprocess.run([qmd] + list(args), capture_output=True, text=True, errors="replace", timeout=timeout, env=env)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "keine Antwort nach %d s" % timeout
    except OSError as ex:
        return -1, str(ex)


def sammlungen(ausgabe):
    """Namen aus `qmd collection list` (Zeilen 'name (qmd://name/)')."""
    return re.findall(r"^(\S+) \(qmd://", ausgabe, re.M)


def plugins_liste(txt):
    """(enabled, disabled) aus config.yaml ohne PyYAML: Block 'plugins:' auf oberster Ebene, Listen inline [a, b] oder als '- a'."""
    erg = {"enabled": [], "disabled": []}
    zeilen = txt.splitlines()
    i = next((n for n, z in enumerate(zeilen) if re.match(r"plugins:\s*(#.*)?$", z)), None)
    if i is None:
        return erg["enabled"], erg["disabled"]
    aktuell = None
    for z in zeilen[i + 1:]:
        if z.strip() and not z[0].isspace() and not z.lstrip().startswith("#"):
            break
        m = re.match(r"\s+(enabled|disabled):\s*(\[(.*)\])?\s*(#.*)?$", z)
        if m:
            aktuell = m.group(1)
            if m.group(2):
                erg[aktuell] += [x.strip().strip("'\"") for x in m.group(3).split(",") if x.strip()]
                aktuell = None
            continue
        m = re.match(r"\s*-\s*([^#\s]+)", z)
        if m and aktuell:
            erg[aktuell].append(m.group(1).strip("'\""))
        elif z.strip() and not z.lstrip().startswith("#"):
            aktuell = None
    return erg["enabled"], erg["disabled"]


def _alter_text(sek):
    if sek < 120:
        return "vor %d s" % sek
    if sek < 7200:
        return "vor %d Min." % (sek // 60)
    return "vor %.1f Std." % (sek / 3600)


def _zeit(t):
    import datetime
    for f in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(t, f)
        except (TypeError, ValueError):
            pass
    try:
        return datetime.datetime.fromisoformat(t)
    except (TypeError, ValueError):
        return None


def _anzahl(x):
    return len(x) if isinstance(x, (list, tuple)) else (x or 0)


def letzter_lauf(pfad, minuten=None, jetzt=None):
    """Punkt 'Letzter Dauerlauf' aus dem Marker {zeit, ordner, neu, geaendert, weg, fehler:[...]} (dauerlauf.py)."""
    import datetime
    name = "Letzter Dauerlauf"
    if not os.path.isfile(pfad):
        return _punkt(name, "offen", "noch kein Lauf (%s fehlt)" % pfad, "Kommt nach dem ersten Lauf des Dauerlaufs von selbst. Sofort: launchctl kickstart gui/$(id -u)/<Label> (Mac).")
    try:
        m = json.load(open(pfad, encoding="utf-8"))
    except (OSError, ValueError) as ex:
        return _punkt(name, "fehlt", "Marker nicht lesbar: %s" % ex)
    z = _zeit(m.get("zeit"))
    alter = None
    if z is not None:
        jetzt = jetzt or (datetime.datetime.now(z.tzinfo) if z.tzinfo else datetime.datetime.now())
        alter = max(0, int((jetzt - z).total_seconds()))
    ordner = m.get("ordner")
    detail = "%s (%s): %d Ordner, %d neu, %d geaendert, %d weg" % (m.get("zeit"), _alter_text(alter) if alter is not None else "Zeit unbekannt",
                                                                _anzahl(ordner), _anzahl(m.get("neu")), _anzahl(m.get("geaendert")), _anzahl(m.get("weg")))
    fehler = m.get("fehler") or []
    if fehler:
        mehr = " (+%d weitere)" % (len(fehler) - 3) if len(fehler) > 3 else ""
        hilfe = "Steht dort 'Ordner nicht lesbar': siehe Punkt 'Ueberwachte Ordner lesbar'. Volles Protokoll: dauerlauf.log in der Datenwurzel."
        return _punkt(name, "fehlt", detail + "; %d Fehler: %s%s" % (len(fehler), " | ".join(str(f) for f in fehler[:3]), mehr), hilfe)
    if minuten and alter is not None and alter > (3 * minuten + 5) * 60:
        return _punkt(name, "fehlt", detail + "; zu alt (Takt alle %d Min.) - der Dauerlauf laeuft offenbar nicht" % minuten,
                      "Installer erneut mit --dauerlauf %d starten (am Mac angemeldet), dann Gesundheitscheck wiederholen." % minuten)
    return _punkt(name, "ok", detail + ", keine Fehler")


def _dienst_geladen(label):
    """(geladen, detail) fuer den Planer: Mac launchctl print gui/<uid>/<label>, Windows schtasks /query; sonst (None, ...)."""
    try:
        if sys.platform == "darwin":
            plist = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", label + ".plist")
            r = subprocess.run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)], capture_output=True, text=True, errors="replace", timeout=20)
            if r.returncode == 0:
                return True, "geladen (%s)" % plist
            return False, ("nicht geladen; %s" % ("plist liegt da: " + plist if os.path.isfile(plist) else "keine plist " + plist))
        if sys.platform == "win32":
            r = subprocess.run(["schtasks", "/query", "/tn", label], capture_output=True, text=True, errors="replace", timeout=20)
            return r.returncode == 0, "geplante Aufgabe %s %s" % (label, "vorhanden" if r.returncode == 0 else "fehlt")
    except (OSError, subprocess.SubprocessError) as ex:
        return False, "nicht pruefbar: %s" % ex
    return None, "kein LaunchAgent auf diesem System (cron)"




def _erreichbar(url):
    try:
        urllib.request.urlopen(url, timeout=3).close()
        return True
    except urllib.error.HTTPError:
        return True  # the service answers (e.g. 401/404)
    except (OSError, ValueError):
        return False


def bericht():
    p = []
    k = konfig.lesen()
    mac = sys.platform == "darwin"
    try:
        py = konfig.python_finden("pypdfium2")
        p.append(_punkt("Python for Trove", "ok", py + (" (from trove.json)" if os.environ.get("TROVE_PYTHON") else " (with pypdfium2)")))
    except RuntimeError as ex:
        py = None
        p.append(_punkt("Python for Trove", "fehlt", str(ex), "Install the packages listed in plugin.yaml (pip install pypdfium2 pikepdf numpy pillow)."))
    try:
        exe = py or konfig.python_finden(None)
        code = "import importlib,json;r={}\nfor m in %r:\n  try: importlib.import_module(m); r[m]=1\n  except Exception: r[m]=0\nprint(json.dumps(r))" % ([m for m, _ in PAKETE + OCR],)
        erg = json.loads(subprocess.run([exe, "-c", code], capture_output=True, text=True, timeout=120, env=konfig.kind_env()).stdout.strip().splitlines()[-1])
        pip = '"%s" -m pip install ' % exe
        for m, name in PAKETE:
            p.append(_punkt("Package " + name, "ok" if erg.get(m) else "fehlt", "importable" if erg.get(m) else "not installed (needed for reading, splitting and Into knowledge)",
                            "" if erg.get(m) else pip + name))
        fehlt_ocr = [name for m, name in OCR if not erg.get(m)]
        p.append(_punkt("Text recognition (scans)", "hinweis" if fehlt_ocr else "ok",
                        ("scans are not read (missing: %s); text PDFs and text files work normally" % ", ".join(fehlt_ocr)) if fehlt_ocr else "rapidocr importable",
                        (pip + " ".join(fehlt_ocr)) if fehlt_ocr else ""))
    except (RuntimeError, OSError, ValueError, IndexError, subprocess.SubprocessError) as ex:
        p.append(_punkt("Packages", "fehlt", "cannot check: %s" % ex))
    w = konfig.datenwurzel()
    try:
        os.makedirs(w, exist_ok=True)
        t = os.path.join(w, ".schreibtest")
        open(t, "w").close()
        os.remove(t)
        p.append(_punkt("Data root writable", "ok", w))
    except OSError as ex:
        p.append(_punkt("Data root writable", "fehlt", "%s: %s" % (w, ex), "Set a writable data folder (TROVE_DATEN or datenwurzel in trove.json)."))
    api = _plugin_api()
    if api:
        soll = laufzeit_module(api, HIER)
        fehlt = [n for n in soll if not os.path.isfile(os.path.join(HIER, n + ".py"))]
        p.append(_punkt("Modules complete", "fehlt" if fehlt else "ok", ("missing: " + ", ".join(fehlt)) if fehlt else "%d modules" % len(soll), "Reinstall the plugin." if fehlt else ""))
    qmd = qmd_finden()
    if not qmd:
        p.append(_punkt("Search tool qmd", "optional", "not found - Into knowledge still writes twins and cards, it just skips the search index",
                        "Optional: npm install -g @tobilu/qmd"))
    else:
        rc, aus = _qmd_lauf(qmd, "--version", timeout=30)
        p.append(_punkt("Search tool qmd", "ok" if rc == 0 else "fehlt", ("%s, %s" % (qmd, aus.splitlines()[0] if aus else "?")) if rc == 0 else "%s does not start: %s" % (qmd, aus[-200:]),
                        "" if rc == 0 else "Is node installed? Reinstall qmd."))
    try:
        import wissenskarte as wk
        ww = wk.wiki_wurzel(anlegen=False)
        p.append(_punkt("Knowledge folder (wiki)", "ok" if os.path.isdir(ww) else "optional", ww + (" exists" if os.path.isdir(ww) else " will be created on the first Into knowledge run"),
                        ""))
    except ValueError as ex:
        p.append(_punkt("Knowledge folder (wiki)", "fehlt", str(ex), "Fix wiki_wurzel in trove.json or unset TROVE_WIKI."))
    ueberwacht = k.get("ueberwacht") or []
    minuten = k.get("dauerlauf_minuten")
    noetig = bool(ueberwacht) or bool(minuten)
    label = "ai.trove.dauerlauf-" + konfig.mandant()
    geladen, det = _dienst_geladen(label)
    if geladen is None:
        p.append(_punkt("Continuous run (scheduler)", "optional", det))
    else:
        p.append(_punkt("Continuous run (scheduler)", "ok" if geladen else ("fehlt" if noetig else "optional"),
                        det if noetig or geladen else "not set up (no watched folders)",
                        "" if geladen else "Optional: python tools/installieren.py ... --dauerlauf 30 (installs an OS scheduler job)"))
    for e in ueberwacht:
        o = e.get("ordner") or ""
        try:
            n = len(os.listdir(o))
            p.append(_punkt("Folder readable: " + o, "ok", "%d entries visible" % n))
        except OSError as ex:
            nicht_da = not os.path.exists(o)
            p.append(_punkt("Folder readable: " + o, "fehlt", "folder does not exist" if nicht_da else "folder not readable (%s)" % ex.strerror,
                            "Moved or renamed? Add it to Into knowledge again." if nicht_da else
                            ("macOS blocks reading: System Settings > Privacy & Security > Full Disk Access > add %s." % (k.get("python") or "the Python used by Trove")) if mac else ""))
    if noetig or os.path.isfile(os.path.join(w, "dauerlauf_letzter_lauf.json")):
        p.append(letzter_lauf(os.path.join(w, "dauerlauf_letzter_lauf.json"), minuten))
    url = os.environ.get("TROVE_STIRLING", "http://127.0.0.1:8090")
    ok = _erreichbar(url)
    p.append(_punkt("Stirling-PDF (Split)", "ok" if ok else "optional", url + (" answers" if ok else " not reachable (optional, only for splitting multi-page PDFs)")))
    p.append(_punkt("hermes CLI (agent tasks)", "ok" if konfig.hermes_exe() else "optional",
                    konfig.hermes_exe() or "not found (PATH, ~/.local/bin; only needed for tasks to agents)"))
    return {"ok": all(x["status"] not in SCHLECHT for x in p), "mandant": konfig.mandant(), "datenwurzel": w, "hermes_home": konfig.hermes_home(), "punkte": p}


def text(b):
    z = ["Trove health: client %s, data %s" % (b["mandant"], b["datenwurzel"])]
    for x in b["punkte"]:
        z.append("  [%-8s] %s: %s%s" % (x["status"], x["name"], x["detail"], ("  ->  " + x["hilfe"]) if x["hilfe"] else ""))
    z.append("OVERALL: " + ("ok" if b["ok"] else "something is missing (see lines marked [fehlt] or [offen])"))
    return "\n".join(z)


if __name__ == "__main__":
    b = bericht()
    print(text(b))
    print(json.dumps(b, ensure_ascii=False))
    sys.exit(0 if b["ok"] else 1)
