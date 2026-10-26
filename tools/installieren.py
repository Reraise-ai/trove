"""Trove-Plugin in Hermes installieren: Plugin-Dateien + alle Laufzeit-Module + trove.json nach <HERMES_HOME>/plugins/trove/dashboard,
Desktop-Oberflaeche nach <HERMES_HOME>/desktop-plugins/trove, Eintrag in config.yaml (plugins.enabled), ohne PyYAML. Idempotent.
HERMES_HOME wie Hermes selbst: Umgebungsvariable, sonst Windows %LOCALAPPDATA%\\hermes, sonst ~/.hermes.
Usage:   python installieren.py --mandant kunde --datenwurzel ~/Trove
         With an existing knowledge wiki: ... --wiki-wurzel ~/Wissen/wiki --qmd-standardsuche
         python installieren.py --pruefen                                  (Gesundheitscheck der Installation)
Python (Mac Standard, sonst mit --venv): eigenes venv ~/.trove-venv aus /opt/homebrew/bin/python3.12 (oder 3.13, oder --python),
Pakete pypdfium2 pikepdf numpy pillow (Pflicht) + onnxruntime rapidocr-onnxruntime (Texterkennung, darf scheitern); offline: --wheels <ordner>.
Nie pip --user. Windows bleibt beim bisherigen Weg (--python oder Suche), ausser --venv wird ausdruecklich gesetzt.
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


def plugins_enabled_setzen(txt):
    """Fuegt 'trove' in plugins.enabled ein (nur Zeilenarbeit, Rest byteidentisch). Liefert (neuer_text, geaendert)."""
    nl = "\r\n" if "\r\n" in txt else "\n"
    z = txt.splitlines(keepends=True)
    ende = lambda s: s.rstrip("\r\n")
    start = next((i for i, s in enumerate(z) if re.match(r"plugins:\s*(#.*)?$", ende(s)) or re.match(r"plugins:\s*\{\s*\}\s*(#.*)?$", ende(s))), None)
    if start is None:
        pre = "" if not txt or txt.endswith(("\n", "\r")) else nl
        return txt + pre + "plugins:" + nl + "  enabled:" + nl + "    - trove" + nl, True
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
        z[start + 1:start + 1] = [ein + "enabled:" + nl, ein + "  - trove" + nl]
        return "".join(z), True
    m = re.match(re.escape(ein) + r"enabled:\s*\[(.*)\]\s*(#.*)?$", ende(z[en]))
    if m:  # Inline-Liste -> Blockliste
        alt = [x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()]
        if "trove" in alt:
            return txt, False
        z[en:en + 1] = [ein + "enabled:" + nl] + [ein + "  - %s%s" % (x, nl) for x in alt + ["trove"]]
        return "".join(z), True
    j, letzter, item_ein = en + 1, en, ein + "  "
    while j < blk_ende and (re.match(r"\s*-\s", z[j]) or re.match(r"\s*-\s*$", ende(z[j])) or not ende(z[j]).strip() or z[j].lstrip().startswith("#")):
        mm = re.match(r"(\s*)-\s*(.*?)\s*(#.*)?$", ende(z[j]))
        if mm and z[j].lstrip().startswith("-"):
            if mm.group(2).strip("'\"") == "trove":  # exakte Zeilenpruefung, nicht "trove-xyz"
                return txt, False
            item_ein, letzter = mm.group(1), j
        j += 1
    z.insert(letzter + 1, item_ein + "- trove" + nl)
    return "".join(z), True


def _py_version(exe):
    r = subprocess.run([exe, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"], capture_output=True, text=True, errors="replace", timeout=60)
    return r.stdout.strip() if r.returncode == 0 else None


def venv_python(ziel):
    return os.path.join(ziel, "Scripts", "python.exe") if sys.platform == "win32" else os.path.join(ziel, "bin", "python")


def basis_python_waehlen(wunsch=None):
    """Python, aus dem das venv entsteht: --python, sonst Homebrew/usr-local python3.12, dann 3.13, dann python3.12/3.13 im PATH."""
    if wunsch:
        w = os.path.expanduser(wunsch)
        if not os.path.isfile(w):
            raise SystemExit("FEHLER: Python nicht gefunden: %s" % w)
        return w
    for p in BASIS_PYTHON + tuple(shutil.which(n) or "" for n in ("python3.12", "python3.13")):
        if p and os.path.isfile(p):
            return p
    raise SystemExit("FEHLER: Kein Python 3.12 oder 3.13 gefunden. Bitte im Terminal ausfuehren:\n"
                     "    brew install python@3.12\n"
                     "und danach den Installer erneut starten. (Mit --python /opt/homebrew/bin/python3.14 geht es auch, "
                     "dann aber ohne Texterkennung fuer eingescannte Belege.)")


def venv_einrichten(basis, ziel, wheels=None, ausgabe=print):
    """Legt das venv an (oder nutzt es weiter) und installiert die Pakete hinein. Liefert (venv-python, texterkennung_ok).
    Pflichtpakete muessen klappen (sonst SystemExit mit Klartext); die Texterkennung darf scheitern."""
    ziel = os.path.abspath(os.path.expanduser(ziel))
    vpy = venv_python(ziel)
    soll = _py_version(basis)
    if not soll:
        raise SystemExit("FEHLER: %s startet nicht. Anderes Python mit --python angeben oder: brew install python@3.12" % basis)
    if tuple(int(x) for x in soll.split(".")) < (3, 9):
        raise SystemExit("FEHLER: %s ist Python %s, Trove braucht mindestens 3.9 (empfohlen 3.12): brew install python@3.12" % (basis, soll))
    ist = _py_version(vpy) if os.path.isfile(vpy) else None
    if ist != soll:
        if os.path.exists(ziel):  # anderes oder kaputtes venv: beiseitelegen, nicht loeschen
            alt = ziel + ".alt-" + time.strftime("%Y%m%d-%H%M%S")
            os.replace(ziel, alt)
            ausgabe("Altes venv beiseitegelegt: %s" % alt)
        r = subprocess.run([basis, "-m", "venv", ziel], capture_output=True, text=True, errors="replace")
        if r.returncode != 0 or not os.path.isfile(vpy):
            raise SystemExit("FEHLER: venv konnte nicht angelegt werden (%s -m venv %s): %s" % (basis, ziel, (r.stdout + r.stderr).strip()[-400:]))
        ausgabe("venv angelegt: %s (Python %s)" % (ziel, soll))
    else:
        ausgabe("venv vorhanden: %s (Python %s)" % (ziel, soll))
    env = dict(os.environ, PIP_USER="0", PIP_DISABLE_PIP_VERSION_CHECK="1")
    quelle = ["--no-index", "--find-links", os.path.abspath(os.path.expanduser(wheels))] if wheels else []
    pip = [vpy, "-m", "pip", "install"] + quelle
    r = subprocess.run(pip + PAKETE_PFLICHT, capture_output=True, text=True, errors="replace", env=env, timeout=3600)
    if r.returncode != 0:
        tipp = "Wheel-Ordner pruefen (passend zu Python %s)." % soll if wheels else "Internet pruefen oder offline mit --wheels <ordner>."
        raise SystemExit("FEHLER: Pakete %s liessen sich nicht installieren. %s\n%s" % (" ".join(PAKETE_PFLICHT), tipp, (r.stdout + r.stderr).strip()[-600:]))
    r = subprocess.run(pip + PAKETE_OCR, capture_output=True, text=True, errors="replace", env=env, timeout=3600)
    ocr = r.returncode == 0
    if not ocr:
        ausgabe("HINWEIS: Texterkennung (%s) nicht installiert - eingescannte Belege werden ohne Texterkennung aufgenommen, "
                "Text-PDFs und Textdateien gehen normal. (Python %s; die Texterkennung braucht Python 3.12.)" % (" ".join(PAKETE_OCR), soll))
    r = subprocess.run([vpy, "-c", "import pypdfium2, pikepdf, numpy, PIL"], capture_output=True, text=True, errors="replace", timeout=120)
    if r.returncode != 0:
        raise SystemExit("FEHLER: Pakete installiert, aber nicht ladbar in %s: %s" % (vpy, (r.stdout + r.stderr).strip()[-400:]))
    ausgabe("Pakete im venv: %s%s" % (" ".join(PAKETE_PFLICHT), (" + " + " ".join(PAKETE_OCR)) if ocr else " (ohne Texterkennung)"))
    return vpy, ocr


def plist_text(label, python, skript, minuten, log, home, pfad=None):
    import plistlib
    return plistlib.dumps({"Label": label, "ProgramArguments": [python, skript], "StartInterval": minuten * 60, "RunAtLoad": False,
                           "StandardOutPath": log, "StandardErrorPath": log,
                           "EnvironmentVariables": {"PATH": pfad or gesundheit.such_pfad(), "HERMES_HOME": home, "PYTHONUTF8": "1"}})


def qmd_eintragen(k, ausgabe=print):
    """Absoluten qmd-Pfad als umgebung.TROVE_QMD in trove.json (nicht auf Windows: dort bleibt die bisherige Suche). Liefert den Pfad oder None."""
    if sys.platform == "win32":
        return None
    um = k.setdefault("umgebung", {})
    q = gesundheit.qmd_finden() or (um.get("TROVE_QMD") if um.get("TROVE_QMD") and os.path.isfile(um["TROVE_QMD"]) else None)
    if q:
        um["TROVE_QMD"] = q
        ausgabe("qmd gefunden: %s (in trove.json als TROVE_QMD)" % q)
    else:
        ausgabe("WARNUNG: qmd nicht gefunden (gesucht: PATH, %s). \"Ins Wissen\" und der Card-Pusher brauchen qmd; "
                "nach der Installation von qmd den Installer erneut starten." % ", ".join(gesundheit.qmd_ordner()))
    return q


def _lc(*args):
    return subprocess.run(["launchctl"] + list(args), capture_output=True, text=True, errors="replace")


def launchagent_laden(label, plist, uid=None, lc=_lc, warte=time.sleep):
    """bootout (tolerant) -> bootstrap gui/<uid> (ein zweiter Versuch) -> launchctl print als Beweis. Liefert (ok, klartext)."""
    dom = "gui/%d" % (os.getuid() if uid is None else uid)
    ziel = dom + "/" + label
    if lc("print", dom).returncode != 0:
        return False, ("Keine Bildschirmanmeldung fuer dieses Konto (launchctl-Bereich %s fehlt), z. B. Installation per SSH ohne angemeldeten Bildschirm. "
                       "Der Dauerlauf ist eingetragen (%s), aber NICHT gestartet. Am Mac mit diesem Konto anmelden und den Installer mit --dauerlauf "
                       "erneut starten." % (dom, plist))
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
            return True, "LaunchAgent geladen und erster Lauf gestartet: %s" % ziel
        warte(2)
    return False, ("LaunchAgent konnte nicht geladen werden (launchctl bootstrap %s %s: rc %d %s). Der Dauerlauf laeuft NICHT."
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
            return weg, ("Dauerlauf abgeschaltet (%s)" % plist) if weg else "Dauerlauf liess sich nicht stoppen (launchctl bootout %s)" % label
        os.makedirs(os.path.dirname(plist), exist_ok=True)
        q = (k.get("umgebung") or {}).get("TROVE_QMD")
        with open(plist, "wb") as f:
            f.write(plist_text(label, python, skript, minuten, log, home, gesundheit.such_pfad(q)))
        ok, txt = launchagent_laden(label, plist)
        return ok, ("Dauerlauf alle %d Min: %s" % (minuten, txt)) if ok else txt
    if sys.platform == "win32":
        if minuten == 0:
            r = subprocess.run(["schtasks", "/delete", "/tn", label, "/f"], capture_output=True, text=True, errors="replace")
        else:
            r = subprocess.run(["schtasks", "/create", "/tn", label, "/tr", '"%s" "%s"' % (python, skript), "/sc", "minute", "/mo", str(minuten), "/f"],
                               capture_output=True, text=True, errors="replace")
        return r.returncode == 0, "Dauerlauf (%s) %s: rc %d %s" % ("aus" if minuten == 0 else "alle %d Min" % minuten, label, r.returncode, (r.stdout + r.stderr).strip()[:200])
    return True, "cron-Zeile: */%d * * * * %s %s >> %s 2>&1" % (minuten, python, skript, log)


def selbsttest_config():
    import plistlib
    p = plistlib.loads(plist_text("ai.trove.dauerlauf-x", "/opt/homebrew/bin/python3", "/h/dauerlauf.py", 30, "/d/dauerlauf.log", "/Users/k/.hermes"))
    pfad = p["EnvironmentVariables"]["PATH"].split(":")
    assert p["StartInterval"] == 1800 and p["ProgramArguments"][1] == "/h/dauerlauf.py", p
    assert {"/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.bun/bin"), "/usr/bin"} <= set(pfad), pfad
    assert gesundheit.such_pfad("/x/qmd").split(":")[0] == "/x" and len(pfad) == len(set(pfad)), pfad
    f = plugins_enabled_setzen
    assert f("plugins:\n  clone_timeout_seconds: 5\nx: 1\n") == ("plugins:\n  enabled:\n    - trove\n  clone_timeout_seconds: 5\nx: 1\n", True)
    assert f("a: 1\n") == ("a: 1\nplugins:\n  enabled:\n    - trove\n", True)
    assert f("plugins:\n  enabled:\n    - trove-xyz\n  other: 1\n") == ("plugins:\n  enabled:\n    - trove-xyz\n    - trove\n  other: 1\n", True)
    assert f("plugins:\n  enabled:\n    - trove\n")[1] is False
    assert f("plugins:\r\n  enabled: [a, b]\r\n") == ("plugins:\r\n  enabled:\r\n    - a\r\n    - b\r\n    - trove\r\n", True)
    assert f("plugins:\n  enabled:\n  - a\nnext: 1\n") == ("plugins:\n  enabled:\n  - a\n  - trove\nnext: 1\n", True)
    assert f("plugins: {}\n") == ("plugins:\n  enabled:\n    - trove\n", True)
    assert f("")[0] == "plugins:\n  enabled:\n    - trove\n"
    print("selbsttest config ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mandant"); ap.add_argument("--datenwurzel"); ap.add_argument("--python"); ap.add_argument("--register-db")
    ap.add_argument("--wiki-wurzel", help="Ordner wiki/ des Wissenssystems (Standard ~/Wissen/wiki)")
    ap.add_argument("--qmd-standardsuche", action="store_true", help="QMD-Sammlungen mit includeByDefault true (Standardsuche findet sie)")
    ap.add_argument("--hermes-home", help="statt HERMES_HOME/Standard"); ap.add_argument("--pruefen", action="store_true")
    ap.add_argument("--dauerlauf", nargs="?", const=30, type=int, metavar="MINUTEN", help="ueberwachte Ordner alle N Minuten nachfuehren (Standard 30; 0 = abschalten): LaunchAgent (Mac) / geplante Aufgabe (Windows)")
    ap.add_argument("--venv", nargs="?", const=VENV_STANDARD, metavar="ORDNER", help="eigenes venv anlegen/nutzen (Mac: Standard ~/.trove-venv); --python ist dann das Basis-Python")
    ap.add_argument("--kein-venv", action="store_true", help="kein venv (Mac); --python wird dann direkt genommen")
    ap.add_argument("--wheels", metavar="ORDNER", help="Pakete offline aus diesem Ordner (pip --no-index --find-links)")
    ap.add_argument("--selbsttest", action="store_true")
    a = ap.parse_args()
    dl_ok, dl_text = True, ""
    if a.selbsttest:
        return selbsttest_config()
    home = os.path.expanduser(a.hermes_home) if a.hermes_home else konfig.hermes_home()
    ziel = os.path.join(home, "plugins", "trove"); dash = os.path.join(ziel, "dashboard")
    if a.pruefen:
        if not os.path.isfile(os.path.join(dash, "gesundheit.py")):
            sys.exit("Nicht installiert: %s fehlt. Erst installieren." % dash)
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
    droot = os.path.join(home, "desktop-plugins", "trove")
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
            bak = cfg + ".bak-vor-trove-" + time.strftime("%Y%m%d-%H%M%S")
            shutil.copyfile(cfg, bak)
        open(cfg, "w", encoding="utf-8", newline="").write(neu)
        print("config.yaml: plugins.enabled += trove (Sicherung %s)" % bak)
    print(json.dumps({"hermes_home": home, "ziel": ziel, "desktop_root": droot, "module": module, "trove_json": k, "config_geaendert": geaendert}, ensure_ascii=False))
    print("Naechster Schritt: Hermes neu starten; Gesundheitscheck: python %s --pruefen" % os.path.abspath(__file__))
    if not dl_ok:
        sys.exit("FEHLER Dauerlauf: " + dl_text)


if __name__ == "__main__":
    main()
