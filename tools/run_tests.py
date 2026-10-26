#!/usr/bin/env python3
"""Run all Trove tests: the modules' built-in self-tests plus a packaging/backend smoke test.

    python tools/run_tests.py

The smoke test copies ONLY the plugin directory (what the Hermes catalog installs) to a temp folder, imports the backend
exactly like Hermes does (importlib, no package context) and exercises it through FastAPI's TestClient. Needs: fastapi, httpx
(Hermes provides them), pypdfium2, pikepdf, numpy, pillow (declared in plugin/plugin.yaml).
"""
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(REPO, "plugin", "dashboard")
SELBSTTESTS = [("belegprotokoll.py", "--selbsttest"), ("gemeinsam.py",), ("konfig.py",), ("oeffentlich.py",), ("wissenskarte.py",),
               ("wissen.py", "--selbsttest")]
EXTRAS = os.path.join(REPO, "extras", "integrations")


def lauf(cmd, cwd, env=None):
    r = subprocess.run([sys.executable, *cmd], cwd=cwd, capture_output=True, text=True, env=env, timeout=900)
    ok = r.returncode == 0
    print(("PASS " if ok else "FAIL ") + " ".join(cmd))
    if not ok:
        print((r.stdout + r.stderr)[-2000:])
    return ok


def main():
    erg = [lauf(list(t), DASH) for t in SELBSTTESTS]
    if os.path.isdir(EXTRAS):
        erg.append(lauf([os.path.join(REPO, "tools", "run_extras_tests.py")], REPO))
    erg.append(lauf([os.path.join(REPO, "tools", "installieren.py"), "--selbsttest"], REPO))
    erg.append(lauf([os.path.join(REPO, "tools", "test_wissen.py")], REPO))
    erg.append(lauf([os.path.join(REPO, "tools", "test_backend.py")], REPO))
    print("ALL PASSED" if all(erg) else "FAILURES: %d" % erg.count(False))
    sys.exit(0 if all(erg) else 1)


if __name__ == "__main__":
    main()
