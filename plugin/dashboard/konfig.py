"""Trove-Konfiguration: Reihenfolge Umgebungsvariable TROVE_* > trove.json (neben diesem Modul) > Standard.

trove.json (vom Installer geschrieben): datenwurzel, mandant, python, register_db, umgebung {TROVE_X: Wert}.
Namen von Geheimnissen (Schluessel, Passwoerter, Tokens) in "umgebung" werden verworfen: die kommen nur aus Hermes.
Beim Import werden die Werte als Umgebungsvariablen vorbelegt (setdefault), damit jedes Werkzeug und jeder Unterprozess
dieselbe Wahrheit sieht. Standard der Datenwurzel: <Benutzerverzeichnis>/Trove.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys

HIER = os.path.dirname(os.path.abspath(__file__))
FELDER = {"datenwurzel": "TROVE_DATEN", "mandant": "TROVE_MANDANT", "python": "TROVE_PYTHON", "register_db": "TROVE_REGISTER_DB",
          "wiki_wurzel": "TROVE_WIKI", "qmd_standardsuche": "TROVE_QMD_STANDARDSUCHE"}
_python_cache = {}


def konfig_pfad():
    """trove.json: TROVE_KONFIG, else next to this module if one is there (legacy installer layout), else in the data root
    (<TROVE_DATEN or ~/Trove>/trove.json) -- never inside the installed plugin package, which a catalog update replaces."""
    if os.environ.get("TROVE_KONFIG"):
        return os.environ["TROVE_KONFIG"]
    neben = os.path.join(HIER, "trove.json")
    if os.path.isfile(neben):
        return neben
    return os.path.join(os.environ.get("TROVE_DATEN") or os.path.join(os.path.expanduser("~"), "Trove"), "trove.json")


def lesen():
    p = konfig_pfad()
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def ueberwacht_eintragen(eintrag):
    """Ordner fuer den Dauerlauf in trove.json vormerken (gleicher Ordner + gleiche Sammlung wird ersetzt)."""
    k = lesen()
    liste = [e for e in k.get("ueberwacht", []) if (e.get("ordner"), e.get("sammlung")) != (eintrag["ordner"], eintrag["sammlung"])] + [eintrag]
    k["ueberwacht"] = liste
    p = konfig_pfad()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p + ".neu", "w", encoding="utf-8") as f:
        json.dump(k, f, ensure_ascii=False, indent=1)
    os.replace(p + ".neu", p)


def eintrag_setzen(name, wert):
    """One top-level entry in trove.json (atomic write); the other entries stay."""
    k = lesen()
    k[name] = wert
    p = konfig_pfad()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p + ".neu", "w", encoding="utf-8") as f:
        json.dump(k, f, ensure_ascii=False, indent=1)
    os.replace(p + ".neu", p)


GEHEIM_MUSTER = re.compile(r"(^|_)(API_?KEY|KEY|SECRET|PASSWORD|PASSWD|PASS|TOKEN|CREDENTIALS?)S?(_|$)", re.I)
_verworfen = []


def ist_geheimname(var):
    """Names that must never come from trove.json: every secret Trove declares (zugang.GEHEIMNISSE) and anything that looks like
    a key, secret, password or token. Those come only from Hermes."""
    try:
        import zugang
        if str(var).upper() in zugang.GEHEIMNISSE:  # case-insensitive: os.environ on Windows is
            return True
    except ImportError:
        pass
    return bool(GEHEIM_MUSTER.search(str(var)))


def umgebung_setzen():
    p = konfig_pfad()
    if os.path.isfile(p):  # pin the file read now: its "datenwurzel" must not move later reads and writes to <datenwurzel>/trove.json
        os.environ["TROVE_KONFIG"] = p  # also over an empty TROVE_KONFIG, which konfig_pfad reads as unset
    k = lesen()
    for feld, var in FELDER.items():
        if k.get(feld):
            os.environ.setdefault(var, str(k[feld]))
    for var, wert in (k.get("umgebung") or {}).items():
        if ist_geheimname(var):
            if var not in _verworfen:  # notice without the value
                _verworfen.append(var)
                logging.getLogger("trove").warning("trove.json 'umgebung': %s ignored - secrets come only from Hermes", var)
            continue
        os.environ.setdefault(var, str(wert))
    os.environ.setdefault("TROVE_DATEN", os.path.join(os.path.expanduser("~"), "Trove"))
    os.environ.setdefault("TROVE_MANDANT", "haus")


umgebung_setzen()


def datenwurzel():
    return os.environ["TROVE_DATEN"]


_AN = ("true", "1", "ja", "yes", "on", "an", "ein")
_AUS = ("false", "0", "nein", "no", "off", "aus")
_gewarnt = set()


def _schalter(wert):
    """(on, unclear) for one raw value. Unknown values count as ON (fail closed)."""
    if wert is None or wert is False or wert is True:
        return bool(wert), False
    t = str(wert).strip().lower() if isinstance(wert, (str, int)) else None
    if t in _AN:
        return True, False
    if t in _AUS:
        return False, False
    return True, True


def freigabe_pflicht_stand():
    """Switch `freigabe_pflicht` (default OFF) as (value, source, warning); source is trove.json | umgebung | standard | fehler.
    ON if trove.json says ON or TROVE_FREIGABE_PFLICHT says ON: the environment can only switch on, never off. Any value not in
    the table (also "" in the file), a trove.json that cannot be read, is no JSON object or is a directory, and a TROVE_KONFIG
    that points to a missing file: ON with a warning (fail closed). An empty TROVE_FREIGABE_PFLICHT counts as not set. Never raises.
    Read on every call, so a change takes effect without a restart."""
    warnung = None
    try:
        p = konfig_pfad()
        k = {}
        if os.path.lexists(p):  # lesen() would treat a directory as "no file"; a dangling link fails in open() -> ON
            with open(p, encoding="utf-8") as f:
                k = json.load(f)
        elif os.environ.get("TROVE_KONFIG"):  # the environment must not switch off by pointing elsewhere
            raise OSError("TROVE_KONFIG points to a missing file")
        if not isinstance(k, dict):
            raise ValueError("not a JSON object")
        datei, unklar = _schalter(k.get("freigabe_pflicht"))
        if unklar:
            warnung = "trove.json freigabe_pflicht=%r is not a known value; treated as ON" % (k.get("freigabe_pflicht"),)
        quelle = "trove.json" if "freigabe_pflicht" in k else "standard"
    except (OSError, ValueError, RecursionError) as e:
        datei, quelle, warnung = True, "fehler", "trove.json unreadable (%s); freigabe_pflicht treated as ON" % e
    umg, unklar = _schalter(os.environ.get("TROVE_FREIGABE_PFLICHT") or None)
    if unklar:
        warnung = "TROVE_FREIGABE_PFLICHT=%r is not a known value; treated as ON" % os.environ.get("TROVE_FREIGABE_PFLICHT")
    if umg and not datei:
        quelle = "umgebung"
    if warnung and warnung not in _gewarnt:
        _gewarnt.add(warnung)
        logging.getLogger("trove").warning(warnung)
    return datei or umg, quelle, warnung


def freigabe_pflicht():
    return freigabe_pflicht_stand()[0]


def mandant():
    return os.environ["TROVE_MANDANT"]


def hermes_home():
    """Wie Hermes selbst (hermes_constants.py): HERMES_HOME, sonst Windows %LOCALAPPDATA%\\hermes, sonst ~/.hermes (jeweils + HERMES_DATA_DIR_SUFFIX)."""
    h = os.environ.get("HERMES_HOME", "").strip()
    if h:
        return os.path.expanduser(h)
    suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
    if sys.platform == "win32":
        basis = os.environ.get("LOCALAPPDATA", "").strip() or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(basis, "hermes" + suffix)
    return os.path.join(os.path.expanduser("~"), ".hermes" + suffix)


def hermes_exe():
    """hermes-Programm: TROVE_HERMES, Windows %LOCALAPPDATA%\\hermes\\bin\\hermes.exe, PATH, ~/.local/bin/hermes (Mac/Linux), /opt/homebrew/bin."""
    fest = [os.environ.get("TROVE_HERMES"), os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes", "bin", "hermes.exe"),
            shutil.which("hermes"), os.path.join(os.path.expanduser("~"), ".local", "bin", "hermes"), "/opt/homebrew/bin/hermes"]
    return next((p for p in fest if p and os.path.isfile(p)), None)


def kind_env(**extra):
    """Umgebung fuer Unterprozesse mit Trove-Python: ohne PYTHONPATH/PYTHONHOME des Elternprozesses. Hermes (Mac, 0.21.5) setzt
    PYTHONPATH auf seine eigenen 3.14-Pakete; das venv-Python 3.12 laedt sonst deren numpy und scheitert."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "__PYVENV_LAUNCHER__")}
    env.update(extra)
    return env


def _kann(exe, modul):
    try:
        return subprocess.run([exe, "-c", "import " + modul], capture_output=True, timeout=60, env=kind_env()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _kandidaten():
    for n in ("python3", "python"):
        w = shutil.which(n)
        if w:
            yield w
    if shutil.which("py"):
        try:
            r = subprocess.run(["py", "-3", "-c", "import sys;print(sys.executable)"], capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and r.stdout.strip():
                yield r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    yield sys.executable


def python_finden(modul=None):
    """Python fuer die Unterprozesse. TROVE_PYTHON (Umgebung oder trove.json) gilt ohne Pruefung; sonst das erste
    python3/python/py im PATH (zuletzt sys.executable), das `modul` (z. B. pypdfium2) importieren kann. RuntimeError mit Klartext."""
    fest = os.environ.get("TROVE_PYTHON")
    if fest:
        if not os.path.exists(fest):
            raise RuntimeError("Python from TROVE_PYTHON/trove.json not found: %s. Correct the path in trove.json (\"python\")." % fest)
        return fest
    if modul in _python_cache:
        return _python_cache[modul]
    for exe in _kandidaten():
        if _kann(exe, modul or "sys"):  # auch ohne Paketwunsch pruefen: der Windows-Store-Platzhalter python3.exe startet nichts
            _python_cache[modul] = exe
            return exe
    raise RuntimeError("Kein Python mit %s gefunden. Einrichten: python tools/installieren.py ... (legt auf dem Mac ~/.trove-venv mit allen Paketen an; "
                       "Windows: py -3 -m pip install pypdfium2 pikepdf rapidocr-onnxruntime numpy pillow) oder in trove.json \"python\" auf ein passendes Python setzen." % modul)


def _selbsttest_geheim():
    import tempfile
    for n in ("TROVE_RERAISE_KEY", "TROVE_DOCUSEAL_API_KEY", "TROVE_ERPNEXT_API_SECRET", "TROVE_IMAP_PASSWORD", "GITHUB_TOKEN", "X_API_KEY",
              "TROVE_GMAIL_CLIENT_ID", "trove_gmail_client_id", "Trove_Reraise_Key", "MY_PASSWORD_FILE"):
        assert ist_geheimname(n), n
    for n in ("TROVE_STIRLING", "TROVE_OLLAMA", "TROVE_QMD", "TROVE_WIKI", "TROVE_HERMES", "TROVE_ASSIGNEE", "TROVE_SKILL", "TROVE_CARD_STATUS",
              "TROVE_ARBEITSORDNER", "TROVE_REGISTER_DB", "TROVE_MONKEYS"):
        assert not ist_geheimname(n), n
    alt = os.environ.get("TROVE_KONFIG")
    with tempfile.TemporaryDirectory() as w:
        p = os.path.join(w, "trove.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"umgebung": {"TROVE_RERAISE_KEY": "FROM-JSON-1", "TROVE_DOCUSEAL_API_KEY": "FROM-JSON-2", "TROVE_STIRLING_TEST": "http://127.0.0.1:9"}}, f)
        os.environ["TROVE_KONFIG"] = p
        for n in ("TROVE_RERAISE_KEY", "TROVE_DOCUSEAL_API_KEY", "TROVE_STIRLING_TEST"):
            os.environ.pop(n, None)
        try:
            umgebung_setzen()
            assert "TROVE_RERAISE_KEY" not in os.environ and "TROVE_DOCUSEAL_API_KEY" not in os.environ
            assert os.environ["TROVE_STIRLING_TEST"] == "http://127.0.0.1:9"
        finally:
            os.environ.pop("TROVE_STIRLING_TEST", None)
            if alt is None:
                os.environ.pop("TROVE_KONFIG", None)
            else:
                os.environ["TROVE_KONFIG"] = alt


def _selbsttest_freigabe():
    import tempfile
    alt = {n: os.environ.get(n) for n in ("TROVE_KONFIG", "TROVE_FREIGABE_PFLICHT", "TROVE_DATEN")}
    with tempfile.TemporaryDirectory() as w:
        p = os.path.join(w, "trove.json")
        os.environ.pop("TROVE_KONFIG", None)
        os.environ["TROVE_DATEN"] = w
        os.environ.pop("TROVE_FREIGABE_PFLICHT", None)

        def datei(inhalt):
            with open(p, "w", encoding="utf-8") as f:
                f.write(inhalt if isinstance(inhalt, str) else json.dumps(inhalt))

        try:
            assert konfig_pfad() == p and freigabe_pflicht_stand() == (False, "standard", None)  # no trove.json at all: default OFF
            datei({"freigabe_pflicht": True})
            assert freigabe_pflicht_stand() == (True, "trove.json", None)
            fehlt = os.path.join(w, "fehlt.json")
            os.environ["TROVE_KONFIG"] = fehlt  # the environment points away from the file that says ON: still ON, never OFF
            w_, q, warnung = freigabe_pflicht_stand()
            assert (w_, q) == (True, "fehler") and "missing file" in warnung, (w_, q, warnung)
            os.symlink(os.path.join(w, "gibt-es-nicht"), fehlt)  # dangling link
            assert freigabe_pflicht_stand()[:2] == (True, "fehler")
            os.environ.pop("TROVE_KONFIG")
            os.remove(p)
            os.symlink(os.path.join(w, "gibt-es-nicht"), p)  # the default trove.json is a dangling link: ON, not "no file"
            assert freigabe_pflicht_stand()[:2] == (True, "fehler")
            os.remove(p)
            os.environ["TROVE_KONFIG"] = p
            datei("[" * 100000 + "]" * 100000)  # nested deeper than the parser can go: ON, no exception
            assert freigabe_pflicht_stand()[:2] == (True, "fehler")
            for wert, erwartet, warn in ((True, True, False), ("ja", True, False), ("an", True, False), ("ein", True, False),
                                         ("AN", True, False), (1, True, False), (" On ", True, False), (False, False, False),
                                         ("0", False, False), ("nein", False, False), ("aus", False, False), (0, False, False),
                                         (2, True, True), ("vielleicht", True, True), ([], True, True), ({}, True, True), (1.5, True, True),
                                         ("", True, True), ("  ", True, True)):
                datei({"freigabe_pflicht": wert})
                w_, q, warnung = freigabe_pflicht_stand()
                assert (w_, q, bool(warnung)) == (erwartet, "trove.json", warn), (wert, w_, q, warnung)
            datei({"freigabe_pflicht": None})  # null = not set = OFF
            assert freigabe_pflicht_stand()[0] is False
            for kaputt in ("[]", "1", "null", "{kaputt"):  # not an object / no JSON: ON, source fehler
                datei(kaputt)
                w_, q, warnung = freigabe_pflicht_stand()
                assert (w_, q) == (True, "fehler") and warnung, (kaputt, w_, q)
            os.remove(p)
            os.mkdir(p)  # trove.json is a directory
            assert freigabe_pflicht_stand()[:2] == (True, "fehler")
            os.rmdir(p)
            datei({"freigabe_pflicht": True})
            os.environ["TROVE_FREIGABE_PFLICHT"] = "0"  # the environment never loosens
            assert freigabe_pflicht_stand() == (True, "trove.json", None)
            datei({"freigabe_pflicht": False})
            for wert in ("1", "true", "JA", "On", "an"):
                os.environ["TROVE_FREIGABE_PFLICHT"] = wert
                assert freigabe_pflicht_stand() == (True, "umgebung", None), wert
            os.environ["TROVE_FREIGABE_PFLICHT"] = "kaputt"
            assert freigabe_pflicht_stand()[:2] == (True, "umgebung") and freigabe_pflicht_stand()[2]
            os.environ["TROVE_FREIGABE_PFLICHT"] = ""  # empty variable = not set = OFF
            assert freigabe_pflicht_stand() == (False, "trove.json", None)
            assert freigabe_pflicht() is False
            # ~/Trove/trove.json says ON and names another datenwurzel: still ON, read from ~/Trove. Child process with its own
            # HOME, because umgebung_setzen runs at import.
            home, nas = os.path.join(w, "home"), os.path.join(w, "nas")
            os.makedirs(os.path.join(home, "Trove"))
            os.makedirs(nas)
            with open(os.path.join(home, "Trove", "trove.json"), "w", encoding="utf-8") as f:
                json.dump({"freigabe_pflicht": True, "datenwurzel": nas}, f)
            env = {k: v for k, v in kind_env(HOME=home, USERPROFILE=home).items() if k not in ("TROVE_KONFIG", "TROVE_DATEN", "TROVE_FREIGABE_PFLICHT")}
            r = subprocess.run([sys.executable, "-I", "-c", "import json, sys; sys.path.insert(0, %r); import konfig; "
                                "print(json.dumps([konfig.freigabe_pflicht_stand()[:2], konfig.konfig_pfad(), konfig.datenwurzel()]))" % HIER],
                               capture_output=True, text=True, env=env, timeout=60)
            assert r.returncode == 0, r.stderr
            assert json.loads(r.stdout) == [[True, "trove.json"], os.path.join(home, "Trove", "trove.json"), nas], r.stdout
            env["TROVE_KONFIG"] = ""  # empty = unset: the pin must still hold, never fall back to <datenwurzel>/trove.json
            r = subprocess.run(r.args, capture_output=True, text=True, env=env, timeout=60)
            assert r.returncode == 0, r.stderr
            assert json.loads(r.stdout) == [[True, "trove.json"], os.path.join(home, "Trove", "trove.json"), nas], r.stdout
        finally:
            for n, v in alt.items():
                if v is None:
                    os.environ.pop(n, None)
                else:
                    os.environ[n] = v


if __name__ == "__main__":
    _selbsttest_geheim()
    _selbsttest_freigabe()
    assert datenwurzel() and mandant()
    assert python_finden() or True
    print("selbsttest ok")
