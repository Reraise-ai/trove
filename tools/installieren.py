"""Developer/offline installer: copy the Trove plugin (all runtime modules + trove.json) to <HERMES_HOME>/plugins/reraise-trove/dashboard,
the desktop UI to <HERMES_HOME>/desktop-plugins/reraise-trove, and add the entry to config.yaml (plugins.enabled), without PyYAML. Idempotent.
Not needed when Hermes installs the plugin from the catalog (that covers the plugin files); this script adds the optional parts:
a private Python venv, the continuous-run scheduler job and the config file.
HERMES_HOME like Hermes itself: environment variable, else Windows %LOCALAPPDATA%\\hermes, else ~/.hermes.
Usage:   python installieren.py --mandant kunde --datenwurzel ~/Trove
         With an existing knowledge wiki: ... --wiki-wurzel ~/Wissen/wiki --qmd-standardsuche
         python installieren.py --pruefen                                  (health check of the installation)
Python (Mac default, otherwise with --venv): private venv ~/.trove-venv from /opt/homebrew/bin/python3.12 (or 3.13, or --python),
packages pypdfium2 pikepdf numpy pillow (required) + onnxruntime rapidocr-onnxruntime (OCR, may fail); offline: --wheels <folder>.
Never pip --user. Windows keeps the previous way (--python or search) unless --venv is set explicitly.
"""
import argparse, json, os, re, shutil, subprocess, sys, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUELLE = os.path.join(REPO, "plugin")  # the self-contained plugin directory (also what the Hermes catalog installs)
sys.path.insert(0, os.path.join(QUELLE, "dashboard"))
_vorher = set(os.environ)
import konfig  # noqa: E402
import gesundheit  # noqa: E402
for _k in set(os.environ) - _vorher:  # konfig belegt TROVE_* aus der Repo-trove.json vor; das darf nicht in die Pruefung der Installation durchschlagen
    del os.environ[_k]

PAKETE_PFLICHT = ["pypdfium2", "pikepdf", "numpy", "pillow"]
PAKETE_OCR = ["onnxruntime", "rapidocr-onnxruntime"]  # rapidocr-onnxruntime verlangt Python < 3.13
BASIS_PYTHON = ("/opt/homebrew/bin/python3.12", "/usr/local/bin/python3.12", "/opt/homebrew/bin/python3.13", "/usr/local/bin/python3.13")
VENV_STANDARD = os.path.join("~", ".trove-venv")
PLUGIN_NAME = "reraise-trove"  # must match plugin.yaml / manifest.json / plugin.js ID


def plugins_enabled_setzen(txt):
    """Adds 'reraise-trove' to plugins.enabled (line editing only, the rest stays byte-identical). Returns (new_text, changed)."""
    nl = "\r\n" if "\r\n" in txt else "\n"
    z = txt.splitlines(keepends=True)
    ende = lambda s: s.rstrip("\r\n")
    start = next((i for i, s in enumerate(z) if re.match(r"plugins:\s*(#.*)?$", ende(s)) or re.match(r"plugins:\s*\{\s*\}\s*(#.*)?$", ende(s))), None)
    if start is None:
        pre = "" if not txt or txt.endswith(("\n", "\r")) else nl
        return txt + pre + "plugins:" + nl + "  enabled:" + nl + "    - " + PLUGIN_NAME + nl, True
    if "{" in z[start]:
        z[start] = "plugins:" + nl
    blk_ende = len(z)
    for i in range(start + 1, len(z)):
        if ende(z[i]).strip() and not z[i][0].isspace() and not z[i].lstrip().startswith("#"):
            blk_ende = i
            break
    kind = next((ende(s) for s in z[start + 1:blk_ende] if ende(s).strip() and not s.lstrip().startswith("#")), None)
    ein = re.match(r"\s*", kind).group() if kind else "  "
    en = next((i for i in range(start + 1, blk_ende) if re.match(re.escape(ein) + r"enabled:\s*(\[.*\])?\s*(#.*)?$", ende(z[i]))), None)
    if en is None:
        z[start + 1:start + 1] = [ein + "enabled:" + nl, ein + "  - " + PLUGIN_NAME + nl]
        return "".join(z), True
    m = re.match(re.escape(ein) + r"enabled:\s*\[(.*)\]\s*(#.*)?$", ende(z[en]))
    if m:  # Inline-Liste -> Blockliste
        alt = [x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()]
        if PLUGIN_NAME in alt:
            return txt, False
        z[en:en + 1] = [ein + "enabled:" + nl] + [ein + "  - %s%s" % (x, nl) for x in alt + [PLUGIN_NAME]]
        return "".join(z), True
    j, letzter, item_ein = en + 1, en, ein + "  "
    while j < blk_ende and (re.match(r"\s*-\s", z[j]) or re.match(r"\s*-\s*$", ende(z[j])) or not ende(z[j]).strip() or z[j].lstrip().startswith("#")):
        mm = re.match(r"(\s*)-\s*(.*?)\s*(#.*)?$", ende(z[j]))
        if mm and z[j].lstrip().startswith("-"):
            if mm.group(2).strip("'\"") == PLUGIN_NAME:  # exakte Zeilenpruefung, nicht "trove-xyz"
                return txt, False
            item_ein, letzter = mm.group(1), j
        j += 1
    z.insert(letzter + 1, item_ein + "- " + PLUGIN_NAME + nl)
    return "".join(z), True


def _py_version(exe):
    r = subprocess.run([exe, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"], capture_output=True, text=True, errors="replace", timeout=60)
    return r.stdout.strip() if r.returncode == 0 else None


def venv_python(ziel):
    return os.path.join(ziel, "Scripts", "python.exe") if sys.platform == "win32" else os.path.join(ziel, "bin", "python")


def basis_python_waehlen(wunsch=None):
    """Python the venv is built from: --python, else Homebrew/usr-local python3.12, then 3.13, then python3.12/3.13 on PATH."""
    if wunsch:
        w = os.path.expanduser(wunsch)
        if not os.path.isfile(w):
            raise SystemExit("ERROR: Python not found: %s" % w)
        return w
    for p in BASIS_PYTHON + tuple(shutil.which(n) or "" for n in ("python3.12", "python3.13")):
        if p and os.path.isfile(p):
            return p
    raise SystemExit("ERROR: No Python 3.12 or 3.13 found. Please run in a terminal:\n"
                     "    brew install python@3.12\n"
                     "and then start the installer again. (--python /opt/homebrew/bin/python3.14 works too, "
                     "but then without OCR for scanned documents.)")


def venv_einrichten(basis, ziel, wheels=None, ausgabe=print):
    """Legt das venv an (oder nutzt es weiter) und installiert die Pakete hinein. Liefert (venv-python, texterkennung_ok).
    Pflichtpakete muessen klappen (sonst SystemExit mit Klartext); die Texterkennung darf scheitern."""
    ziel = os.path.abspath(os.path.expanduser(ziel))
    vpy = venv_python(ziel)
    soll = _py_version(basis)
    if not soll:
        raise SystemExit("ERROR: %s does not start. Give another Python with --python or: brew install python@3.12" % basis)
    if tuple(int(x) for x in soll.split(".")) < (3, 9):
        raise SystemExit("ERROR: %s is Python %s, Trove needs at least 3.9 (3.12 recommended): brew install python@3.12" % (basis, soll))
    ist = _py_version(vpy) if os.path.isfile(vpy) else None
    if ist != soll:
        if os.path.exists(ziel):  # anderes oder kaputtes venv: beiseitelegen, nicht loeschen
            alt = ziel + ".alt-" + time.strftime("%Y%m%d-%H%M%S")
            os.replace(ziel, alt)
            ausgabe("Altes venv beiseitegelegt: %s" % alt)
        r = subprocess.run([basis, "-m", "venv", ziel], capture_output=True, text=True, errors="replace")
        if r.returncode != 0 or not os.path.isfile(vpy):
            raise SystemExit("ERROR: could not create the venv (%s -m venv %s): %s" % (basis, ziel, (r.stdout + r.stderr).strip()[-400:]))
        ausgabe("venv created: %s (Python %s)" % (ziel, soll))
    else:
        ausgabe("venv vorhanden: %s (Python %s)" % (ziel, soll))
    env = dict(os.environ, PIP_USER="0", PIP_DISABLE_PIP_VERSION_CHECK="1")
    quelle = ["--no-index", "--find-links", os.path.abspath(os.path.expanduser(wheels))] if wheels else []
    pip = [vpy, "-m", "pip", "install"] + quelle
    r = subprocess.run(pip + PAKETE_PFLICHT, capture_output=True, text=True, errors="replace", env=env, timeout=3600)
    if r.returncode != 0:
        tipp = "Check the wheel folder (it must match Python %s)." % soll if wheels else "Check the internet connection or go offline with --wheels <folder>."
        raise SystemExit("ERROR: packages %s could not be installed. %s\n%s" % (" ".join(PAKETE_PFLICHT), tipp, (r.stdout + r.stderr).strip()[-600:]))
    r = subprocess.run(pip + PAKETE_OCR, capture_output=True, text=True, errors="replace", env=env, timeout=3600)
    ocr = r.returncode == 0
    if not ocr:
        ausgabe("NOTE: OCR (%s) is not installed - scanned documents are taken in without text recognition, "
                "text PDFs and text files work normally. (Python %s; OCR needs Python 3.12.)" % (" ".join(PAKETE_OCR), soll))
    r = subprocess.run([vpy, "-c", "import pypdfium2, pikepdf, numpy, PIL"], capture_output=True, text=True, errors="replace", timeout=120)
    if r.returncode != 0:
        raise SystemExit("ERROR: packages installed but not importable in %s: %s" % (vpy, (r.stdout + r.stderr).strip()[-400:]))
    ausgabe("Packages in the venv: %s%s" % (" ".join(PAKETE_PFLICHT), (" + " + " ".join(PAKETE_OCR)) if ocr else " (without OCR)"))
    return vpy, ocr


def plist_text(label, python, skript, minuten, log, home, pfad=None):
    import plistlib
    return plistlib.dumps({"Label": label, "ProgramArguments": [python, skript], "StartInterval": minuten * 60, "RunAtLoad": False,
                           "StandardOutPath": log, "StandardErrorPath": log,
                           "EnvironmentVariables": {"PATH": pfad or gesundheit.such_pfad(), "HERMES_HOME": home, "PYTHONUTF8": "1"}})


def qmd_eintragen(k, ausgabe=print):
    """Absolute qmd path as umgebung.TROVE_QMD in trove.json (not on Windows: the previous search stays there). Returns the path or None."""
    if sys.platform == "win32":
        return None
    um = k.setdefault("umgebung", {})
    q = gesundheit.qmd_finden() or (um.get("TROVE_QMD") if um.get("TROVE_QMD") and os.path.isfile(um["TROVE_QMD"]) else None)
    if q:
        um["TROVE_QMD"] = q
        ausgabe("qmd found: %s (stored in trove.json as TROVE_QMD)" % q)
    else:
        ausgabe("WARNING: qmd not found (searched: PATH, %s). \"Into knowledge\" indexes with qmd (without it files are still read and cards written, the index step is skipped); "
                "after installing qmd, run the installer again." % ", ".join(gesundheit.qmd_ordner()))
    return q


def _lc(*args):
    return subprocess.run(["launchctl"] + list(args), capture_output=True, text=True, errors="replace")


def launchagent_laden(label, plist, uid=None, lc=_lc, warte=time.sleep):
    """bootout (tolerant) -> bootstrap gui/<uid> (ein zweiter Versuch) -> launchctl print als Beweis. Liefert (ok, klartext)."""
    dom = "gui/%d" % (os.getuid() if uid is None else uid)
    ziel = dom + "/" + label
    if lc("print", dom).returncode != 0:
        return False, ("No graphical login for this account (launchctl domain %s is missing), e.g. installation over SSH without a logged-in screen. "
                       "The continuous run is written (%s) but NOT started. Log in at the Mac with this account and run the installer with --dauerlauf "
                       "again." % (dom, plist))
    r = None
    for versuch in range(2):
        lc("bootout", ziel)  # tolerant: war evtl. nicht geladen
        for _ in range(20):  # bootout ist asynchron
            if lc("print", ziel).returncode != 0:
                break
            warte(0.5)
        r = lc("bootstrap", dom, plist)
        if r.returncode == 0 and lc("print", ziel).returncode == 0:
            lc("kickstart", ziel)  # erster Lauf jetzt statt erst nach dem Intervall
            return True, "LaunchAgent loaded and first run started: %s" % ziel
        warte(2)
    return False, ("LaunchAgent could not be loaded (launchctl bootstrap %s %s: rc %d %s). The continuous run is NOT running."
                   % (dom, plist, r.returncode, (r.stdout + r.stderr).strip()[:200]))


def launchagent_entladen(label, uid=None, lc=_lc):
    ziel = "gui/%d/%s" % (os.getuid() if uid is None else uid, label)
    lc("bootout", ziel)
    return lc("print", ziel).returncode != 0


def dauerlauf_einrichten(minuten, dash, k, home):
    """Planer fuer dauerlauf.py: macOS LaunchAgent, Windows geplante Aufgabe, sonst cron-Zeile. 0 = abschalten (Plist wird nur umbenannt).
    Liefert (ok, klartext); ok False = der Dauerlauf laeuft nicht (Installer endet dann mit Exit 1)."""
    label = "ai.trove.dauerlauf-" + k.get("mandant", "haus")
    skript = os.path.join(dash, "dauerlauf.py")
    try:
        python = k.get("python") or konfig.python_finden("pypdfium2")
    except RuntimeError:
        python = konfig.python_finden(None)  # ohne pypdfium2 laufen Textdateien; PDFs melden dann Fehler im Lauf
    log = os.path.join(os.path.expanduser(k["datenwurzel"]), "dauerlauf.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    if sys.platform == "darwin":
        plist = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", label + ".plist")
        if minuten == 0:
            weg = launchagent_entladen(label)
            if os.path.exists(plist):
                os.replace(plist, plist + ".aus")
            return weg, ("Continuous run switched off (%s)" % plist) if weg else "The continuous run could not be stopped (launchctl bootout %s)" % label
        os.makedirs(os.path.dirname(plist), exist_ok=True)
        q = (k.get("umgebung") or {}).get("TROVE_QMD")
        with open(plist, "wb") as f:
            f.write(plist_text(label, python, skript, minuten, log, home, gesundheit.such_pfad(q)))
        ok, txt = launchagent_laden(label, plist)
        return ok, ("Continuous run every %d min: %s" % (minuten, txt)) if ok else txt
    if sys.platform == "win32":
        if minuten == 0:
            r = subprocess.run(["schtasks", "/delete", "/tn", label, "/f"], capture_output=True, text=True, errors="replace")
        else:
            r = subprocess.run(["schtasks", "/create", "/tn", label, "/tr", '"%s" "%s"' % (python, skript), "/sc", "minute", "/mo", str(minuten), "/f"],
                               capture_output=True, text=True, errors="replace")
        return r.returncode == 0, "Continuous run (%s) %s: rc %d %s" % ("off" if minuten == 0 else "every %d min" % minuten, label, r.returncode, (r.stdout + r.stderr).strip()[:200])
    return True, "cron-Zeile: */%d * * * * %s %s >> %s 2>&1" % (minuten, python, skript, log)


def selbsttest_config():
    import plistlib
    p = plistlib.loads(plist_text("ai.trove.dauerlauf-x", "/opt/homebrew/bin/python3", "/h/dauerlauf.py", 30, "/d/dauerlauf.log", "/Users/k/.hermes"))
    pfad = p["EnvironmentVariables"]["PATH"].split(":")
    assert p["StartInterval"] == 1800 and p["ProgramArguments"][1] == "/h/dauerlauf.py", p
    assert {"/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.bun/bin"), "/usr/bin"} <= set(pfad), pfad
    assert gesundheit.such_pfad("/x/qmd").split(":")[0] == "/x" and len(pfad) == len(set(pfad)), pfad
    f = plugins_enabled_setzen
    assert f("plugins:\n  clone_timeout_seconds: 5\nx: 1\n") == ("plugins:\n  enabled:\n    - reraise-trove\n  clone_timeout_seconds: 5\nx: 1\n", True)
    assert f("a: 1\n") == ("a: 1\nplugins:\n  enabled:\n    - reraise-trove\n", True)
    assert f("plugins:\n  enabled:\n    - trove-xyz\n  other: 1\n") == ("plugins:\n  enabled:\n    - trove-xyz\n    - reraise-trove\n  other: 1\n", True)
    assert f("plugins:\n  enabled:\n    - reraise-trove\n")[1] is False
    assert f("plugins:\r\n  enabled: [a, b]\r\n") == ("plugins:\r\n  enabled:\r\n    - a\r\n    - b\r\n    - reraise-trove\r\n", True)
    assert f("plugins:\n  enabled:\n  - a\nnext: 1\n") == ("plugins:\n  enabled:\n  - a\n  - reraise-trove\nnext: 1\n", True)
    assert f("plugins: {}\n") == ("plugins:\n  enabled:\n    - reraise-trove\n", True)
    assert f("")[0] == "plugins:\n  enabled:\n    - reraise-trove\n"
    print("selbsttest config ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mandant"); ap.add_argument("--datenwurzel"); ap.add_argument("--python"); ap.add_argument("--register-db")
    ap.add_argument("--wiki-wurzel", help="wiki/ folder of your knowledge system (default ~/Wissen/wiki)")
    ap.add_argument("--qmd-standardsuche", action="store_true", help="QMD collections with includeByDefault true (the default search finds them)")
    ap.add_argument("--hermes-home", help="instead of HERMES_HOME/default"); ap.add_argument("--pruefen", action="store_true")
    ap.add_argument("--dauerlauf", nargs="?", const=30, type=int, metavar="MINUTEN", help="follow the watched folders every N minutes (default 30; 0 = switch off): LaunchAgent (Mac) / scheduled task (Windows)")
    ap.add_argument("--venv", nargs="?", const=VENV_STANDARD, metavar="ORDNER", help="create/use a private venv (Mac default ~/.trove-venv); --python is then the base Python")
    ap.add_argument("--kein-venv", action="store_true", help="no venv (Mac); --python is then used directly")
    ap.add_argument("--wheels", metavar="ORDNER", help="install packages offline from this folder (pip --no-index --find-links)")
    ap.add_argument("--selbsttest", action="store_true")
    a = ap.parse_args()
    dl_ok, dl_text = True, ""
    if a.selbsttest:
        return selbsttest_config()
    home = os.path.expanduser(a.hermes_home) if a.hermes_home else konfig.hermes_home()
    ziel = os.path.join(home, "plugins", PLUGIN_NAME); dash = os.path.join(ziel, "dashboard")
    if a.pruefen:
        if not os.path.isfile(os.path.join(dash, "gesundheit.py")):
            sys.exit("Not installed: %s is missing. Install first." % dash)
        sys.exit(subprocess.run([sys.executable, os.path.join(dash, "gesundheit.py")], env=dict(os.environ, HERMES_HOME=home)).returncode)
    # Copy the whole plugin directory (modules live in plugin/dashboard); the desktop half goes to desktop-plugins/ below.
    module = sorted(n[:-3] for n in os.listdir(os.path.join(QUELLE, "dashboard")) if n.endswith(".py"))
    for wurzel, _dirs, dateien in os.walk(QUELLE):
        if "__pycache__" in wurzel or os.path.relpath(wurzel, QUELLE).split(os.sep)[0] == "desktop":
            continue
        for n in dateien:
            if n.endswith(".pyc"):
                continue
            z = os.path.join(ziel, os.path.relpath(os.path.join(wurzel, n), QUELLE))
            os.makedirs(os.path.dirname(z), exist_ok=True)
            shutil.copyfile(os.path.join(wurzel, n), z)
    kpfad = os.path.join(dash, "trove.json")
    k = json.load(open(kpfad, encoding="utf-8")) if os.path.isfile(kpfad) else {}
    venv = a.venv or (VENV_STANDARD if sys.platform == "darwin" and not a.kein_venv else None)
    if venv:
        vpy, ocr = venv_einrichten(basis_python_waehlen(a.python), venv, a.wheels)
        k["python"], a.python = vpy, None
        k["texterkennung"] = ocr
    elif a.python:
        a.python = os.path.abspath(os.path.expanduser(a.python))
    qmd_eintragen(k)
    for feld, wert in (("mandant", a.mandant), ("datenwurzel", a.datenwurzel), ("python", a.python), ("register_db", a.register_db),
                        ("wiki_wurzel", a.wiki_wurzel), ("qmd_standardsuche", "1" if a.qmd_standardsuche else None)):
        if wert:
            k[feld] = os.path.expanduser(wert) if feld in ("datenwurzel", "wiki_wurzel") else wert
    k.setdefault("mandant", "haus"); k.setdefault("datenwurzel", os.path.join(os.path.expanduser("~"), "Trove"))
    with open(kpfad, "w", encoding="utf-8") as f:
        json.dump(k, f, ensure_ascii=False, indent=1)
    # Desktop-Haelfte als REINES Disk-Plugin in den App-Root (Hot-Load, standardmaessig AN).
    # KEIN .hermes-package.json: mit Marker gilt in der App "opt-in" (defaultEnabled=false, runtime-loader.ts),
    # dann bleibt der Tab aus, bis jemand ihn unter Capabilities -> Plugins einschaltet (Befund 18.10. 02:55).
    droot = os.path.join(home, "desktop-plugins", PLUGIN_NAME)
    os.makedirs(droot, exist_ok=True)
    shutil.copyfile(os.path.join(QUELLE, "desktop", "plugin.js"), os.path.join(droot, "plugin.js"))
    for alt in (os.path.join(droot, ".hermes-package.json"), os.path.join(ziel, "desktop", "plugin.js")):
        if os.path.exists(alt):
            os.remove(alt)
    try:
        os.rmdir(os.path.join(ziel, "desktop"))
    except OSError:
        pass
    if a.dauerlauf is not None:
        k["dauerlauf_minuten"] = a.dauerlauf
        with open(kpfad, "w", encoding="utf-8") as f:
            json.dump(k, f, ensure_ascii=False, indent=1)
        dl_ok, dl_text = dauerlauf_einrichten(a.dauerlauf, dash, k, home)
        print(dl_text)
    cfg = os.path.join(home, "config.yaml")
    txt = open(cfg, encoding="utf-8", newline="").read() if os.path.isfile(cfg) else ""
    neu, geaendert = plugins_enabled_setzen(txt)
    if geaendert:
        bak = None
        if txt:
            bak = cfg + ".bak-before-trove-" + time.strftime("%Y%m%d-%H%M%S")
            shutil.copyfile(cfg, bak)
        open(cfg, "w", encoding="utf-8", newline="").write(neu)
        print("config.yaml: plugins.enabled += " + PLUGIN_NAME + " (backup %s)" % bak)
    print(json.dumps({"hermes_home": home, "ziel": ziel, "desktop_root": droot, "module": module, "trove_json": k, "config_geaendert": geaendert}, ensure_ascii=False))
    print("Next step: restart Hermes; health check: python %s --pruefen" % os.path.abspath(__file__))
    if not dl_ok:
        sys.exit("ERROR continuous run: " + dl_text)


if __name__ == "__main__":
    main()
