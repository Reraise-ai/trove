"""Trove-Gesundheitscheck: Python, Pakete, Datenwurzel, Module, qmd + Sammlungen raw/wiki, Wiki-Ordner, Hermes-Plugins trove + card-pusher,
Dauerlauf (Planer geladen, ueberwachte Ordner lesbar, letzter Lauf aus <datenwurzel>/dauerlauf_letzter_lauf.json), Dienste.
GESAMT "in Ordnung" nur ohne [fehlt]/[offen]. Pflicht im Kundenmodus (kartenziel wiki): raw/wiki, Wiki-Ordner, card-pusher. Aufruf: python gesundheit.py
Auch als GET /api/plugins/trove/gesundheit und `python installieren.py --pruefen`. Installiert nichts, nennt nur den Befehl."""
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
DIENSTE = (("Stirling-PDF (Zerlegen)", "TROVE_STIRLING", "http://127.0.0.1:8090"), ("Paperless (Suche)", "TROVE_PAPERLESS", "http://127.0.0.1:8010"),
           ("DocuSeal (Unterschriften)", "TROVE_DOCUSEAL", "http://127.0.0.1:8091"), ("ERPNext (Spiegel)", "TROVE_ERPNEXT", "http://127.0.0.1:8080"))


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


def _erreichbar(url):
    try:
        urllib.request.urlopen(url, timeout=3).close()
        return True
    except urllib.error.HTTPError:
        return True  # Dienst antwortet (z. B. 401/404)
    except (OSError, ValueError):
        return False


def _erpnext(url):
    """ERPNext gilt nur als 'antwortet', wenn /api/method/ping {"message": "pong"} liefert (auf 8080 kann z. B. signal-cli lauschen)."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/method/ping", timeout=3) as r:
            if json.loads(r.read(4096).decode("utf-8", "replace") or "{}").get("message") == "pong":
                return "ok", url + " antwortet als ERPNext (ping: pong)"
    except (OSError, ValueError, AttributeError):
        pass
    if _erreichbar(url):
        return "optional", url + " antwortet, aber nicht als ERPNext (anderer Dienst auf dem Port) - ERPNext nicht gefunden (optional)"
    return "optional", url + " nicht erreichbar (optional, spaetere Stufen)"


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


def bericht():
    p = []
    k = konfig.lesen()
    wiki_modus = konfig.kartenziel() == "wiki"
    mac = sys.platform == "darwin"
    venv_hilfe = ("Einrichten mit dem Installer (legt %s mit allen Paketen an): python3 tools/installieren.py --mandant <name> --datenwurzel ~/Trove ..."
                  % os.path.join(os.path.expanduser("~"), ".trove-venv"))
    try:
        py = konfig.python_finden("pypdfium2")  # TROVE_PYTHON/trove.json gilt ohne Paketpruefung -> Pakete stehen in den Zeilen darunter
        p.append(_punkt("Python fuer Trove", "ok", py + (" (aus trove.json)" if os.environ.get("TROVE_PYTHON") else " (mit pypdfium2)")))
    except RuntimeError as ex:
        py = None
        p.append(_punkt("Python fuer Trove", "fehlt", str(ex), "" if sys.platform == "win32" else venv_hilfe))
    try:
        exe = py or konfig.python_finden(None)
        code = "import importlib,json;r={}\nfor m in %r:\n  try: importlib.import_module(m); r[m]=1\n  except Exception: r[m]=0\nprint(json.dumps(r))" % ([m for m, _ in PAKETE + OCR],)
        erg = json.loads(subprocess.run([exe, "-c", code], capture_output=True, text=True, timeout=120, env=konfig.kind_env()).stdout.strip().splitlines()[-1])
        pip = '"%s" -m pip install ' % exe
        for m, name in PAKETE:
            p.append(_punkt("Paket " + name, "ok" if erg.get(m) else "fehlt", "ladbar" if erg.get(m) else "nicht installiert (noetig fuer Lesen/Zerlegen/Ins Wissen)",
                            "" if erg.get(m) else (venv_hilfe if mac else pip + name)))
        fehlt_ocr = [name for m, name in OCR if not erg.get(m)]
        p.append(_punkt("Texterkennung (Scans)", "hinweis" if fehlt_ocr else "ok",
                        ("Scans ohne Texterkennung (fehlt: %s); Text-PDFs und Textdateien gehen normal" % ", ".join(fehlt_ocr)) if fehlt_ocr else "rapidocr ladbar",
                        ("braucht Python 3.12: brew install python@3.12, dann Installer erneut" if mac else pip + " ".join(fehlt_ocr)) if fehlt_ocr else ""))
    except (RuntimeError, OSError, ValueError, IndexError, subprocess.SubprocessError) as ex:
        p.append(_punkt("Pakete", "fehlt", "nicht pruefbar: %s" % ex))
    w = konfig.datenwurzel()
    try:
        os.makedirs(w, exist_ok=True)
        t = os.path.join(w, ".schreibtest")
        open(t, "w").close()
        os.remove(t)
        p.append(_punkt("Datenwurzel schreibbar", "ok", w))
    except OSError as ex:
        p.append(_punkt("Datenwurzel schreibbar", "fehlt", "%s: %s" % (w, ex), "datenwurzel in trove.json aendern"))
    api = _plugin_api()
    if api:
        repo = HIER
        soll = laufzeit_module(api, repo)
        fehlt = [n for n in soll if not os.path.isfile(os.path.join(repo, n + ".py"))]
        p.append(_punkt("Module vollstaendig", "fehlt" if fehlt else "ok", ("fehlen: " + ", ".join(fehlt)) if fehlt else "%d Module" % len(soll),
                        "installieren.py erneut ausfuehren" if fehlt else ""))
    # Suche (qmd) und Wissenssystem
    qmd = qmd_finden()
    if not qmd:
        p.append(_punkt("Suchprogramm qmd", "fehlt", "nicht gefunden (gesucht: TROVE_QMD, PATH, %s)" % ", ".join(qmd_ordner()),
                        "qmd installieren (npm install -g @tobilu/qmd), dann Installer erneut starten"))
    else:
        rc, aus = _qmd_lauf(qmd, "--version", timeout=30)
        p.append(_punkt("Suchprogramm qmd", "ok" if rc == 0 else "fehlt", ("%s, %s" % (qmd, aus.splitlines()[0] if aus else "?")) if rc == 0 else "%s startet nicht: %s" % (qmd, aus[-200:]),
                        "" if rc == 0 else "node fehlt? which node; qmd neu installieren"))
        if rc == 0:
            rc, aus = _qmd_lauf(qmd, "collection", "list", timeout=60)
            da = sammlungen(aus) if rc == 0 else []
            noetig = ["raw", "wiki"] if wiki_modus else []
            fehlt = [s for s in noetig if s not in da]
            p.append(_punkt("Sammlungen im Wissen", "fehlt" if (fehlt or rc != 0) else "ok",
                            ("vorhanden: %s%s" % (", ".join(da) or "keine", ("; es fehlen: " + ", ".join(fehlt)) if fehlt else "")) if rc == 0 else "qmd collection list: " + aus[-200:],
                            ("Das Wissenssystem braucht die Sammlungen raw und wiki (qmd collection add ~/Wissen/raw --name raw, ebenso wiki)" if fehlt else "")))
    if wiki_modus:
        ww = os.path.expanduser(os.environ.get("TROVE_WIKI") or k.get("wiki_wurzel") or os.path.join("~", "Wissen", "wiki"))
        ok = os.path.isdir(ww)
        p.append(_punkt("Wiki-Ordner", "ok" if ok else "fehlt", ww + (" vorhanden" if ok else " nicht gefunden"), "" if ok else "wiki_wurzel in trove.json pruefen (Installer --wiki-wurzel)"))
    # Hermes: Plugins eingeschaltet
    home = konfig.hermes_home()
    cfg = os.path.join(home, "config.yaml")
    try:
        an, aus_ = plugins_liste(open(cfg, encoding="utf-8").read())
    except OSError:
        an, aus_ = [], []
    for name, pflicht in (("trove", True), ("card-pusher", wiki_modus)):
        ordner = os.path.isdir(os.path.join(home, "plugins", name))
        aktiv = name in an and name not in aus_ and ordner
        p.append(_punkt("Hermes-Plugin " + name, "ok" if aktiv else ("fehlt" if pflicht else "optional"),
                        "eingeschaltet" if aktiv else "%s%s%s" % ("nicht in plugins.enabled" if name not in an else "", "; in plugins.disabled" if name in aus_ else "",
                                                                    "; Ordner %s fehlt" % os.path.join(home, "plugins", name) if not ordner else ""),
                        "" if aktiv else ("Installer erneut ausfuehren" if name == "trove" else "card-pusher nach ~/.hermes/plugins/card-pusher kopieren, dann: hermes plugins enable card-pusher")))
    # Dauerlauf
    ueberwacht = k.get("ueberwacht") or []
    minuten = k.get("dauerlauf_minuten")
    noetig = bool(ueberwacht) or bool(minuten)
    label = "ai.trove.dauerlauf-" + konfig.mandant()
    geladen, det = _dienst_geladen(label)
    if geladen is None:
        p.append(_punkt("Dauerlauf (Planer)", "optional", det))
    else:
        p.append(_punkt("Dauerlauf (Planer)", "ok" if geladen else ("fehlt" if noetig else "optional"),
                        det if noetig or geladen else "nicht eingerichtet (keine ueberwachten Ordner)",
                        "" if geladen else "am Mac angemeldet: python3 tools/installieren.py ... --dauerlauf 30"))
    for e in ueberwacht:
        o = e.get("ordner") or ""
        try:
            n = len(os.listdir(o))
            p.append(_punkt("Ordner lesbar: " + o, "ok", "%d Eintraege sichtbar" % n))
        except OSError as ex:
            nicht_da = not os.path.exists(o)
            p.append(_punkt("Ordner lesbar: " + o, "fehlt", "Ordner nicht vorhanden" if nicht_da else "Ordner nicht lesbar (%s)" % ex.strerror,
                            "Ordner wurde verschoben/umbenannt? Neu ins Wissen aufnehmen." if nicht_da else
                            "macOS erlaubt das Lesen nicht: Systemeinstellungen > Datenschutz & Sicherheit > Festplattenvollzugriff > + > %s hinzufuegen "
                            "(Befehl+Umschalt+G, Pfad einfuegen), dann Gesundheitscheck wiederholen." % (k.get("python") or "das Trove-Python")))
    if noetig or os.path.isfile(os.path.join(w, "dauerlauf_letzter_lauf.json")):
        p.append(letzter_lauf(os.path.join(w, "dauerlauf_letzter_lauf.json"), minuten))
    for name, var, std in DIENSTE:
        url = os.environ.get(var, std)
        if var == "TROVE_ERPNEXT":
            p.append(_punkt(name, *_erpnext(url)))
            continue
        ok = _erreichbar(url)
        p.append(_punkt(name, "ok" if ok else "optional", url + (" antwortet" if ok else " nicht erreichbar (optional, spaetere Stufen)")))
    rcl = shutil.which("rclone") or next((x for x in ("/opt/homebrew/bin/rclone", "/usr/local/bin/rclone") if os.path.isfile(x)), None) or os.environ.get("TROVE_RCLONE")
    p.append(_punkt("rclone (Online-Drives)", "ok" if rcl else "optional", rcl or "nicht gefunden",
                    "" if rcl else "brew install rclone (Mac) bzw. winget install Rclone.Rclone (Windows)"))
    p.append(_punkt("hermes-Programm (Auftraege)", "ok" if konfig.hermes_exe() else "optional",
                    konfig.hermes_exe() or "nicht gefunden (PATH, ~/.local/bin; nur fuer Auftraege an die KI)"))
    return {"ok": all(x["status"] not in SCHLECHT for x in p), "mandant": konfig.mandant(), "datenwurzel": w, "hermes_home": home,
            "kartenziel": "wiki" if wiki_modus else "brett", "punkte": p}


def text(b):
    z = ["Trove-Gesundheit: Mandant %s, Daten %s" % (b["mandant"], b["datenwurzel"])]
    for x in b["punkte"]:
        z.append("  [%-8s] %s: %s%s" % (x["status"], x["name"], x["detail"], ("  ->  " + x["hilfe"]) if x["hilfe"] else ""))
    z.append("GESAMT: " + ("in Ordnung" if b["ok"] else "es fehlt etwas (siehe Zeilen mit [fehlt] oder [offen])"))
    return "\n".join(z)


if __name__ == "__main__":
    b = bericht()
    print(text(b))
    print(json.dumps(b, ensure_ascii=False))
    sys.exit(0 if b["ok"] else 1)
