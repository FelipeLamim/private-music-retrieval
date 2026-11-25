#!/usr/bin/env bash
# End-to-end demonstration:
#   1. checks the catalog, starts the PIR server and the trusted client (loopback only);
#   2. retrieves one track through the trusted client's API (the same path the browser uses),
#      checks the PCM against the manifest SHA-256 and prints the statistics;
#   3. prints the UI URL and keeps both services running until Ctrl-C (unless --once).
# Usage: scripts/demo.sh [--catalog data/catalog/mini|full] [--track N] [--once]
#        [--server-port 8700] [--client-port 8701] [-- extra pmr.server options]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -L)"
cd "$ROOT"
source ./env.sh

CATALOG=data/catalog/full TRACK=3 ONCE=0 SPORT=8700 CPORT=8701
SERVER_EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --catalog) CATALOG=$2; shift ;;
    --track) TRACK=$2; shift ;;
    --once) ONCE=1 ;;
    --server-port) SPORT=$2; shift ;;
    --client-port) CPORT=$2; shift ;;
    --) shift; SERVER_EXTRA=("$@"); break ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

[ -f "$CATALOG/manifest.json" ] || { echo "missing $CATALOG (run scripts/setup.sh)"; exit 2; }
RUN="$ROOT/.tmp/demo-$$"
mkdir -p "$RUN" logs
PIDS=()
cleanup() {
  for pid in "${PIDS[@]}"; do kill -TERM "$pid" 2>/dev/null || true; done
  for pid in "${PIDS[@]}"; do wait "$pid" 2>/dev/null || true; done
  rm -rf "$RUN"
  echo "[demo] stopped"
}
trap cleanup EXIT INT TERM

echo "[demo] starting PIR server on 127.0.0.1:$SPORT with $CATALOG"
.venv/bin/python -m pmr.server --catalog "$CATALOG" --port "$SPORT" --ready-file "$RUN/server.json" \
  ${SERVER_EXTRA[@]+"${SERVER_EXTRA[@]}"} > logs/demo_server.log 2>&1 &
PIDS+=($!)
for _ in $(seq 1 600); do [ -f "$RUN/server.json" ] && break; sleep 0.2; done
[ -f "$RUN/server.json" ] || { echo "[demo] server failed:"; tail logs/demo_server.log; exit 1; }
tail -1 logs/demo_server.log

echo "[demo] starting trusted client on 127.0.0.1:$CPORT (guard: never opens files under data/catalog)"
.venv/bin/python -m pmr.client --server "http://127.0.0.1:$SPORT" --port "$CPORT" --forbid-reads data/catalog \
  --ready-file "$RUN/client.json" > logs/demo_client.log 2>&1 &
PIDS+=($!)
for _ in $(seq 1 300); do [ -f "$RUN/client.json" ] && break; sleep 0.2; done
[ -f "$RUN/client.json" ] || { echo "[demo] client failed:"; tail logs/demo_client.log; exit 1; }

.venv/bin/python - "$RUN/client.json" "$CATALOG" "$TRACK" <<'EOF'
import hashlib, json, sys, time, urllib.request, io, wave
ready, catalog, track = json.load(open(sys.argv[1])), sys.argv[2], int(sys.argv[3])
base, tok = f"http://127.0.0.1:{ready['listen']['port']}", ready["token"]
def call(path, data=None):
    req = urllib.request.Request(base + path, data=data, headers={"X-PMR-Token": tok, "Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=600)
manifest = json.load(open(f"{catalog}/manifest.json"))
info = json.load(call("/api/catalog"))
t = info["catalog"]["tracks"][track]
print(f"[demo] protocol: {info['backend']['label']}  R={info['layout']['num_records']} tracks x B={info['layout']['num_blocks']} blocks of {info['layout']['block_bytes']} B")
print(f"[demo] retrieving track {track} '{t['title']}' ({t['duration_s']:.1f} s) privately; the server sees only ciphertexts")
job = json.load(call("/api/play", json.dumps({"track": track, "mode": "progressive"}).encode()))
t0 = time.perf_counter()
wav = call(f"/api/jobs/{job['id']}/audio.wav").read()
w = wave.open(io.BytesIO(wav)); pcm = w.readframes(w.getnframes())
ok = hashlib.sha256(pcm).hexdigest() == manifest["tracks"][track]["sha256_pcm"]
while json.load(call(f"/api/jobs/{job['id']}"))["state"] == "running":
    time.sleep(0.1)
st = json.load(call(f"/api/jobs/{job['id']}"))
r = st["result"]
print(f"[demo] exact PCM equality with the catalog: {ok}")
print(f"[demo] first playable audio after {st['first_block_s']*1e3:.0f} ms; full retrieval {r['timings_ms']['total_ms']/1e3:.2f} s "
      f"for {st['audio_s_total']:.1f} s of audio; received {r['bytes']['wire_rx']/1e6:.1f} MB, sent {r['bytes']['wire_tx']/1e6:.2f} MB")
wire = json.load(call("/api/wire"))["entries"]
print("[demo] last requests seen by the PIR server:")
for e in wire[-4:]:
    print(f"         {e['method']:6s} {e['path']}  ({e['request_bytes']} B up, {e['response_bytes']} B down)")
sys.exit(0 if ok else 1)
EOF
URL=$(.venv/bin/python -c "import json,sys;print(json.load(open(sys.argv[1]))['url'])" "$RUN/client.json")
echo
echo "[demo] UI: $URL"
echo "       (WSL2: open the same URL in the Windows browser; remote machine: use an SSH tunnel, see README.md)"
if [ "$ONCE" = 1 ]; then exit 0; fi
echo "[demo] press Ctrl-C to stop"
wait
