#!/usr/bin/env python3
"""Self-tests of the optional integrations in extras/integrations (not part of the plugin package)."""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EX = os.path.join(REPO, "extras", "integrations")


def main():
    ok = True
    for name in ("reraise_hochladen.py", "spiegel_erpnext.py"):
        r = subprocess.run([sys.executable, os.path.join(EX, name), "--selbsttest"], cwd=EX, capture_output=True, text=True, timeout=600)
        print(("PASS " if r.returncode == 0 else "FAIL ") + "extras/" + name + " --selbsttest")
        if r.returncode:
            print((r.stdout + r.stderr)[-1500:])
            ok = False
    for name in ("mail_abholer.py", "docuseal.py"):
        r = subprocess.run([sys.executable, os.path.join(EX, name), "--help"], cwd=EX, capture_output=True, text=True, timeout=60)
        print(("PASS " if r.returncode == 0 else "FAIL ") + "extras/" + name + " --help (imports resolve)")
        ok = ok and r.returncode == 0
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
