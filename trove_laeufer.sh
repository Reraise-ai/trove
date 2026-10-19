#!/bin/bash
# Trove Komplett-Laeufer (laeuft auf der Quellmaschine): pusht alle indexierten Dateien der Quelle
# blockweise verifiziert zum Server. Selbstueberwachend (Heartbeat, Block-Retry). Idempotent.
# NUR lesen auf der Quellmaschine; nichts hier aendern/loeschen.
set -u
# Konfiguration per Umgebungsvariablen (alle ohne Standardwert ausser dem Schluessel):
#   TROVE_SERVER        user@host des Zielservers (Windows)
#   TROVE_SCHLUESSEL    SSH-Schluessel (Standard ~/.ssh/id_ed25519)
#   TROVE_REGISTER_WIN  Registerdatei auf dem Server (Windows-Pfad)
#   TROVE_ZIEL_WIN      Zielordner auf dem Server (Windows-Pfad)
#   TROVE_REPO_WIN      Trove-Repo auf dem Server (Windows-Pfad)
#   TROVE_QUELLORT      Wert der Spalte ort im Register (Standard quelle)
SRV="${TROVE_SERVER:?TROVE_SERVER fehlt}"
KEY="${TROVE_SCHLUESSEL:-$HOME/.ssh/id_ed25519}"
ORT="${TROVE_QUELLORT:-quelle}"
SSHO=(-i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o ServerAliveInterval=20 -o ServerAliveCountMax=5)
DBWIN="${TROVE_REGISTER_WIN:?TROVE_REGISTER_WIN fehlt}"
ZIEL="${TROVE_ZIEL_WIN:?TROVE_ZIEL_WIN fehlt}"
REPOWIN="${TROVE_REPO_WIN:?TROVE_REPO_WIN fehlt}"
PROT="$ZIEL\\belegprotokoll.jsonl"
LOG=/tmp/trove_lauf.log; HB=/tmp/trove_heartbeat; BD=/tmp/trove_blocks
exec >>"$LOG" 2>&1
echo "=== TROVE LAUF start $(date -Is) ==="
date -Is > "$HB"
DBL=/tmp/reg_z.db
[ -f "$DBL" ] || { echo "FEHLER: Index $DBL fehlt auf der Quellmaschine"; exit 1; }
python3 - "$DBL" "$BD" "$ORT" <<'PY'
import sqlite3, sys, os, collections
db, bd, ort = sys.argv[1], sys.argv[2], sys.argv[3]
rows = [r[0] for r in sqlite3.connect(db).execute("select pfad from datei where ort=?", (ort,))]
blk = collections.defaultdict(list)
for p in rows:
    blk['/'.join(p.strip('/').split('/')[:4])].append(p)
os.makedirs(bd, exist_ok=True)
with open(bd + '/_index', 'w') as idx:
    for i, (k, ps) in enumerate(sorted(blk.items())):
        fn = bd + '/b%03d' % i
        open(fn, 'w').write('\n'.join(ps) + '\n')
        idx.write('%s\t%d\t%s\n' % (fn, len(ps), k))
print('bloecke', len(blk), 'dateien', len(rows))
PY
while IFS=$'\t' read -r fn n k; do
  echo "--- block $k ($n dateien) $(date -Is) ---"
  ok=0
  for try in 1 2 3; do
    date -Is > "$HB"
    if tar -cf - -T "$fn" 2>/dev/null | ssh "${SSHO[@]}" "$SRV" "python $REPOWIN\\kopieren.py --db $DBWIN --quell-prefix / --ziel $ZIEL --protokoll $PROT"; then
      echo "block $k OK (try $try) $(date -Is)"; ok=1; break
    else
      echo "block $k FEHLER try $try $(date -Is)"; sleep 5
    fi
  done
  [ "$ok" = 1 ] || echo "BLOCK $k NACH 3 VERSUCHEN OFFEN"
done < "$BD/_index"
date -Is > "$HB"
echo "=== TROVE LAUF fertig $(date -Is) ==="
