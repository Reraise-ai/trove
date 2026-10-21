"""Trove-Konfiguration: Reihenfolge Umgebungsvariable TROVE_* > trove.json (neben diesem Modul) > Standard.

trove.json (vom Installer geschrieben): datenwurzel, mandant, python, register_db, umgebung {TROVE_X: Wert}.
Beim Import werden die Werte als Umgebungsvariablen vorbelegt (setdefault), damit jedes Werkzeug und jeder Unterprozess
dieselbe Wahrheit sieht. Standard der Datenwurzel: <Benutzerverzeichnis>/Trove.
"""
import json
import os
import shutil
import subprocess
import sys

HIER = os.path.dirname(os.path.abspath(__file__))
FELDER = {"datenwurzel": "TROVE_DATEN", "mandant": "TROVE_MANDANT", "python": "TROVE_PYTHON", "register_db": "TROVE_REGISTER_DB",
          "kartenziel": "TROVE_KARTENZIEL", "wiki_wurzel": "TROVE_WIKI", "qmd_standardsuche": "TROVE_QMD_STANDARDSUCHE",
          "reraise_knopf": "TROVE_RERAISE_KNOPF"}
_python_cache = {}


def konfig_pfad():
    return os.environ.get("TROVE_KONFIG") or os.path.join(HIER, "trove.json")


def kartenziel():
    """Die EINE Stelle fuer das Kartenziel (fail-closed): nur ein ausdrueckliches "brett" (Gross/Klein und Leerzeichen egal)
    ist das interne Brett; nicht gesetzt, leer oder jeder andere Wert ist der Kundenmodus "wiki"."""
    return "brett" if os.environ.get("TROVE_KARTENZIEL", "").strip().lower() == "brett" else "wiki"


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
    with open(p + ".neu", "w", encoding="utf-8") as f:
        json.dump(k, f, ensure_ascii=False, indent=1)
    os.replace(p + ".neu", p)


def umgebung_setzen():
    k = lesen()
    for feld, var in FELDER.items():
        if k.get(feld):
            os.environ.setdefault(var, str(k[feld]))
    for var, wert in (k.get("umgebung") or {}).items():
        os.environ.setdefault(var, str(wert))
    os.environ.setdefault("TROVE_DATEN", os.path.join(os.path.expanduser("~"), "Trove"))
    os.environ.setdefault("TROVE_MANDANT", "haus")


umgebung_setzen()


def datenwurzel():
    return os.environ["TROVE_DATEN"]


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
    python3/python/py im PATH (zuletzt sys.executable), das `modul` (z. B. fitz) importieren kann. RuntimeError mit Klartext."""
    fest = os.environ.get("TROVE_PYTHON")
    if fest:
        if not os.path.exists(fest):
            raise RuntimeError("Python aus TROVE_PYTHON/trove.json nicht gefunden: %s. Pfad in trove.json (\"python\") korrigieren." % fest)
        return fest
    if modul in _python_cache:
        return _python_cache[modul]
    for exe in _kandidaten():
        if _kann(exe, modul or "sys"):  # auch ohne Paketwunsch pruefen: der Windows-Store-Platzhalter python3.exe startet nichts
            _python_cache[modul] = exe
            return exe
    raise RuntimeError("Kein Python mit %s gefunden. Einrichten: python plugin/installieren.py ... (legt auf dem Mac ~/.trove-venv mit allen Paketen an; "
                       "Windows: py -3 -m pip install pymupdf rapidocr-onnxruntime numpy pillow) oder in trove.json \"python\" auf ein passendes Python setzen." % modul)


if __name__ == "__main__":
    assert datenwurzel() and mandant()
    assert python_finden() or True
    print("selbsttest ok")
