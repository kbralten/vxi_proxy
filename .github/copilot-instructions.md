Repository onboarding notes for an automated coding agent
======================================================

Purpose
-------
This file tells a coding agent how this repository is structured, how to build,
test, lint, run, and debug changes locally and in CI. Follow these instructions
before making code edits. Trust these instructions first; only search the repo
if the steps here are incomplete or fail.

High-level summary
------------------
- What this repo does: a VXI‑11 protocol gateway that proxies VXI‑11 RPC to
  backend adapters (SCPI, MODBUS, USBTMC). It includes a small browser GUI
  served by aiohttp for editing config and mappings, and a tiny user‑space
  portmapper shim for rpcbind GETPORT.
- Languages & runtime: Python 3.11+, small static HTML/JS SPA (no build step).
- Layout: source lives under `src/vxi_proxy` (PEP-420 package layout).

Quick checklist for local work (always follow in order)
----------------------------------------------------
1. Create and activate a Python 3.11 venv from repo root:

   powershell
   & .\.venv\Scripts\Activate.ps1

2. Install runtime/test deps (always run this after venv activation):

   pip install -r requirements.txt

3. Run lint and quick fixes (recommended):

   # Ruff lint (fatal F class causes CI failure)
   ruff check --select F .
   # optionally auto-fix safe issues
   ruff check --select F . --fix

4. Run unit tests (pytest):

   python -m pytest -q

5. Run the GUI or server for manual testing:

   # GUI only
   python scripts/run_gui_standalone.py

   # GUI + in-process facade (best-effort wiring)
   python scripts/run_gui_with_facade.py

   # Start the façade + optional portmapper for local VXI testing
   python scripts/start_with_portmapper.py

Notes and common pitfalls
-------------------------
- PYTHONPATH: scripts assume `src/` is on your path; runner scripts add it
  automatically. When running ad-hoc modules, ensure you run from repo root
  or add `src/` to PYTHONPATH.
- Port 111: binding the portmapper to TCP/UDP 111 requires elevated privileges.
  For local testing, run the portmapper on a high port (e.g. 11111) via
  `--portmap-port` and adjust client settings.
- Config file: `config.yaml` is the canonical runtime config. The container
  entrypoint will create a minimal default config if none exists. For local
  testing always provide a `config.yaml` (or `config.example.yaml` will be used).
- INTR channel: the minimal portmapper and server intentionally stub INTR
  (interrupt) support; GETPORT returns 0 for the INTR program. CORE and ASYNC
  map to the same TCP port.

Project layout and key files (priority order)
---------------------------------------------
- `src/vxi_proxy/server.py` — main façade implementation:
  - `Vxi11ServerFacade`, `run_from_cli(config_path)` is the main entrypoint.
  - `AsyncRuntime` runs an asyncio loop in a background thread.
  - `Vxi11CoreServer` implements the DEVICE_CORE program.
- `src/vxi_proxy/config.py` — YAML parsing/dataclasses for server, devices,
  and mappings. Use `load_config()`/`save_config()` to validate changes.
- `src/vxi_proxy/gui_server.py` — aiohttp server that serves SPA and exposes
  `/api/config` and `/api/reload` endpoints used by the GUI.
- `src/vxi_proxy/portmapper.py` — tiny user-space portmapper that answers
  PMAPPROC_GETPORT for VXI‑11 programs (CORE, ASYNC) and returns server.port.
- `src/vxi_proxy/static/gui/` — SPA static assets (index.html, styles.css, app.js).
- `scripts/` — useful run/debug helpers:
  - `run_gui_standalone.py`, `run_gui_with_facade.py`, `start_with_portmapper.py`,
    `docker_entrypoint.py`, `ci_probe.py` (CI probe uses xdrlib to validate GETPORT).
- `Dockerfile` and `.github/workflows/docker-build.yml` — containerization & CI
  that build the image and run the portmapper probe as a validation step.

Build & CI specifics (replicate locally)
----------------------------------------
- CI workflow: `.github/workflows/docker-build.yml` builds multi-arch image
  (linux/amd64, linux/arm64) with buildx and then runs a local container and
  executes `/app/scripts/ci_probe.py` inside it to validate the portmapper.
- To emulate CI locally: build the image and run the container, then exec the
  probe script:

  docker build -t vxi-proxy:ci-test .
  docker run -d --name vxi-ci -e PORTMAPPER_ENABLED=1 vxi-proxy:ci-test
  docker exec vxi-ci python /app/scripts/ci_probe.py

  If the probe fails, `docker logs vxi-ci` contains server/portmapper output.

Validation gates that commonly cause PR rejections
--------------------------------------------------
- Ruff F-class issues (use `ruff check --select F .`) — CI fails these.
- Python syntax/type errors — run `python -m py_compile` on modified files
  or run `python -m pytest` to exercise runtime errors.
- Docker build errors: historically a `COPY config.yaml` step failed when
  `config.yaml` was absent; current Dockerfile no longer copies a missing
  config, and the entrypoint will create a default config at runtime.

Best practices for a coding agent
--------------------------------
1. Always run the sequence in "Quick checklist" before opening PRs.
2. Use `load_config()` and `save_config()` to validate config edits; do not
   hand-write YAML unless covered by tests.
3. Prefer small, focused changes and add unit tests for behavior changes.
4. If you modify long‑running threads or loops, add or adjust clean shutdown
   hooks; tests/CI may hang otherwise.
5. When editing runtime port or rpc behavior, update `scripts/ci_probe.py` and
   CI if you change program numbers or protocol behavior.

When to search the repo
-----------------------
Only search if the instructions above are missing the required information for
the task (for example, you need to find a helper script not documented here
or the CI workflow name changed). Start with `grep` for filenames listed
above before broad searching.

If anything here is out of date: prefer the on-disk `README.md`, `Dockerfile`,
and `.github/workflows/*` as the canonical sources and update this file as you
discover differences.

End of instructions.
