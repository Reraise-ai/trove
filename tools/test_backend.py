#!/usr/bin/env python3
"""Packaging + backend smoke test. Copies only plugin/ to a temp dir and drives the real FastAPI router (no network, no Hermes)."""
import importlib.util
import os
import shutil
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    tmp = tempfile.mkdtemp(prefix="trove-pkg-")
    try:
        plugin = os.path.join(tmp, "plugins", "trove")
        shutil.copytree(os.path.join(REPO, "plugin"), plugin, ignore=shutil.ignore_patterns("__pycache__"))  # ONLY the plugin dir
        os.environ["TROVE_DATEN"] = os.path.join(tmp, "data")
        os.environ["HOME"] = os.environ["USERPROFILE"] = os.path.join(tmp, "home")
        os.environ.pop("TROVE_KONFIG", None)
        os.makedirs(os.environ["HOME"])
        api = os.path.join(plugin, "dashboard", "plugin_api.py")
        spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_trove", api)  # as Hermes loads it
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(mod.router, prefix="/api/plugins/trove")
        c = TestClient(app)
        u = "/api/plugins/trove"
        st = c.get(u + "/status")
        assert st.status_code == 200 and st.json()["ok"], st.text
        for name in ("konfig", "belegprotokoll", "aenderungen", "gesundheit", "gemeinsam"):
            assert os.path.dirname(os.path.abspath(sys.modules[name].__file__)) == os.path.dirname(api), name  # imported from the copy
        import backend_checks
        backend_checks.run(c, u, tmp, mod)
        print("backend smoke ok")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
