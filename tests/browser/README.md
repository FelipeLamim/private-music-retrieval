# Headless-browser playback test

Automated test of the real UI playback path: a headless Chromium (Chrome Headless Shell, driven by
Playwright for Python) loads the page served by the trusted client (`python -m pmr.client`), clicks
"Ouvir", plays the WAV stream that the client builds from decrypted PIR blocks, and checks the
result against the catalog manifest. Measured results: [results/browser/](../../results/browser/).

Everything stays inside the project. The test uses its own virtualenv `.venv-browser`, so the main
`.venv` and `requirements.lock` are never touched. Browser binaries live in `.tools/ms-playwright`
(`PLAYWRIGHT_BROWSERS_PATH`, set by `env.sh`). Nothing is installed system-wide and
`playwright install-deps` (sudo/apt) is never run.

## Setup (once)

```bash
cd <project root>
bash tests/browser/setup.sh          # or: scripts/browser_test.sh --setup
```

`setup.sh` does the following:

1. Creates `.venv-browser` with `.tools/bin/uv venv --python 3.10 .venv-browser`.
2. Installs `requirements-browser.lock` with `--require-hashes --only-binary :all:` (playwright 1.65.2, greenlet 3.5.6).
3. Checks whether the Node.js bundled in the Playwright wheel runs on this host. It needs glibc ≥ 2.28
   and fails on Amazon Linux 2 (glibc 2.26). In that case the script downloads the same Node.js
   version built for glibc 2.17 from the Node.js project's unofficial-builds
   (`node-v26.13.1-linux-x64-glibc-217.tar.xz`), checks the pinned SHA-256 and extracts it to
   `.tools/node-v26.13.1-linux-x64-glibc-217/`. The test then points `PLAYWRIGHT_NODEJS_PATH` at that build.
   On hosts where the bundled Node.js works (e.g. Ubuntu 22.04/24.04, WSL), nothing is downloaded.
4. Runs `python -m playwright install chromium-headless-shell`, which downloads Chrome Headless Shell
   159.0.8154.6 (Playwright revision 1255) into `.tools/ms-playwright`. Playwright also fetches its
   ffmpeg build (`ffmpeg-1013`), which is only used for video recording and is not used here.
5. Prints the highest GLIBC symbol version the browser needs and any `ldd` "not found" entries.

Settings shared by the scripts (Node.js version, URL, SHA-256, paths) are in `browser_env.sh`.

## Run

```bash
scripts/browser_test.sh                      # all tests, real-time playback (playbackRate 1)
scripts/browser_test.sh -- --playback-rate 4 # faster T2 playback (the rate is recorded in the JSON)
scripts/browser_test.sh --no-full            # no full-catalog services (T5 then cancels on the mini catalog)
scripts/browser_test.sh -- --no-extras       # skip the probes X1–X3
```

The script performs these steps:

1. It starts `pmr.server` for `data/catalog/mini` and `data/catalog/full`, then one `pmr.client` per
   server with `--forbid-reads data/catalog`. Every service binds 127.0.0.1 with `--port 0`, and the
   script reads the port from the `--ready-file`. All of them run under `nice -n 10`.
2. It runs `run_browser_playback.py` with `.venv-browser/bin/python`.
3. It stops every process it started: SIGTERM, then SIGKILL after 10 s, and only for recorded PIDs.
   On exit, Ctrl-C or SIGTERM it also stops any leftover browser process whose `--user-data-dir`
   lies in the run's own temporary directory.
4. It exits 0 when all required tests pass, 1 when a required test fails or a service died or the
   read guard fired, and 2 on a setup or infrastructure error.

A full run takes about 40 s, including about 12 s of real-time playback.

Outputs:

| Path | Content |
|---|---|
| `results/browser/browser_playback_results.json` | all checks and measured values of the last run (strict JSON) |
| `results/browser/runs/browser_playback_<UTC>.json` | copy of each run |
| `logs/browser/runner.log` | console output of the test runner |
| `logs/browser/ui_after_playback.png` | full-page screenshot after the progressive playback (T7) |
| `logs/browser/ui_complete_mode.png`, `ui_after_cancel.png`, `ui_small_screen.png`, `x1_*.png`, `x3_*.png` | other screenshots (`ui_small_screen.png`: 390 px layout, T8) |
| `logs/browser/browser_requests.log`, `page_console.log` | every request the page made; console messages |
| `logs/browser/service_*.log` | stdout/stderr of the PIR servers and clients |

The per-launch UI token is removed (`t=<redacted>`) from all logs and results. The ready files that
contain it stay in `.tmp/browser/run-*/` and are deleted at the end of the run (`--keep-tmp` keeps them).

### Runner options (after `--`)

`--playback-rate R` (T2, default 1.0), `--track I` (T2/T3, default: shortest mini track),
`--complete-track I` (T4, default: second shortest), `--cancel-track I` (T5, default: longest),
`--no-extras`, `--browser-arg FLAG` (repeatable), `--results PATH`, `--logs-dir DIR`, `--deadline-s S` (default 600).

## Tests

| Id | What is checked |
|---|---|
| T1 | the printed URL opens the landing screen (`#sobre`: title "Private Music Listening", action "Começar a ouvir", player hidden); the action opens `#ouvir`; 10 rows `#row-0..9` with "Ouvir" buttons and manifest titles; `#proto` shows the server's BFV parameters (compared with `/v1/pir/params` from the PIR ready file) |
| T2 | progressive mode: trusted click on "Ouvir", `playing`, `currentTime > 2 s`, `ended`; job status (`first_block_s`, `playback.startup_s`, `stalls`, result timings and bytes) |
| T3 | in-page `fetch` of the same `audio.wav?t=…` URL; SHA-256 (crypto.subtle) of the PCM bytes; `OfflineAudioContext(1, n, 16000).decodeAudioData`, conversion back to int16, SHA-256 compared with `sha256_pcm` |
| T4 | complete mode: no `src` (and `NETWORK_EMPTY`) until the job is `complete`, then playback starts; the play/pause toggle `#btn-toggle` pauses ("Pausada.") and resumes |
| T5 | full catalog: `#btn-cancel` while the retrieval runs, job state becomes `cancelled`, UI shows "cancelada" |
| T6 | `#wire` rows: only whitelisted PIR paths with random tokens and block indices; no title, id or index of any track; block schedule independent of the track |
| T6b | `/api/wire`: identical request sequence for two different tracks |
| T7 | screenshot and JSON written; the metric cards "Blocos recebidos" and "Dados recebidos" equal the job status |
| T8 | UI states and layout: without the token the player shows the access error and no tracks; header navigation switches between "Como funciona" and "Ouvir"; no horizontal overflow at 390 px; track buttons visible at 390 px; visible (solid) keyboard focus; screenshot `logs/browser/ui_small_screen.png` |
| X1–X3 | probes for UI bugs and browser behaviour. They are reported as `bug_reproduced`, `not_reproduced` or `observed` and never change the exit code. Details are recorded in the results JSON. |

Implementation notes:

- The page's CSP (`script-src 'self'`) forbids `eval`. The test therefore only passes function
  predicates to `wait_for_function`/`evaluate`, which Playwright runs through the DevTools protocol.
  `bypass_csp` is not used.
- The in-page instrumentation added with `add_init_script` only registers listeners: media events,
  a MutationObserver on `#audio[src]`, 50 ms state samples and clicks. Probe X1 is the exception: it
  clicks `#btn-cancel` through the UI's own handler as soon as the UI enables it.
- Browser flags added by the test are `--autoplay-policy=no-user-gesture-required`,
  `--disable-audio-output` (fake/null audio output) and
  `--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1` (no DNS except loopback). Playwright
  adds its defaults (`--headless`, `--mute-audio`, `--no-sandbox`, …). The exact command line is
  recorded in the results JSON.
- Each run uses a fresh temporary profile created by Playwright and deleted on close. No existing
  browser profile is used.
- No file in this directory matches pytest's `test_*.py` pattern, so the main `pytest` run (which
  uses `.venv`) does not collect these tests.

## Regenerating the lock file

```bash
source ./env.sh
.tools/bin/uv pip compile tests/browser/requirements-browser.in --python .venv-browser/bin/python \
  --generate-hashes --only-binary :all: -o tests/browser/requirements-browser.lock
```

When the Playwright version changes, update `PW_NODE_VERSION` and `PW_NODE_SHA256` in
`browser_env.sh`. The required Node.js version is the one bundled in the new wheel
(`strings .venv-browser/lib/python3.10/site-packages/playwright/driver/node | grep -m1 node/v`).
The SHA-256 is listed in the release's `SHASUMS256.txt` on unofficial-builds.nodejs.org.

## Manual check in a desktop browser (optional)

Start a PIR server and a trusted client, then open the URL that the client prints
(`http://127.0.0.1:<port>/?t=<token>`) in a browser on the same machine:

```bash
source ./env.sh
.venv/bin/python -m pmr.server --catalog data/catalog/mini --port 0 --ready-file .tmp/pir_ready.json &
# wait for the ready line, read listen.port from .tmp/pir_ready.json, then:
.venv/bin/python -m pmr.client --server http://127.0.0.1:<PIRPORT> --port 0 --forbid-reads data/catalog
```

Stop both processes with Ctrl-C or `kill <pid>` afterwards. The client only binds 127.0.0.1 and
rejects non-loopback `Host` headers. From a Windows machine you can reach it through an SSH local
forward (`ssh -L 8701:127.0.0.1:<port> <host>`) and open `http://localhost:8701/?t=<token>`.
