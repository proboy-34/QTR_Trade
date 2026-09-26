"""QTR_Trade local launcher (used by RUN_QTR.bat / STOP_QTR.bat / STATUS_QTR.bat).

Standard library only. It starts the EXISTING application exactly as documented:

  backend : <venv python> -m uvicorn app.main:app --host 127.0.0.1 --port 8000   (no --reload)
  frontend: npm run dev -- --strictPort       (Vite, 127.0.0.1:5173 - this laptop only, not the LAN)

It never reads or prints API keys, never edits .env, never resets the database, and only
stops processes it started itself (their IDs are kept in .qtr/launcher.json).

Usage:  python scripts/qtr_launcher.py start [--no-browser] | stop | status
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
STATE_DIR = ROOT / ".qtr"
STATE_FILE = STATE_DIR / "launcher.json"
LOG_DIR = ROOT / "logs"
WINDOWS = os.name == "nt"

# Ports and URLs as configured in the project (README, Dockerfile, frontend/vite.config.ts).
BACKEND_HOST, BACKEND_PORT = "127.0.0.1", 8000
FRONTEND_PORT = 5173
BACKEND_URL = f"http://{BACKEND_HOST}:{BACKEND_PORT}"
FRONTEND_URL = f"http://127.0.0.1:{FRONTEND_PORT}"
BACKEND_TITLE, FRONTEND_TITLE = "QTR_Trade Backend", "QTR_Trade Frontend"
HELPERS = {BACKEND_TITLE: "qtr_backend.cmd", FRONTEND_TITLE: "qtr_frontend.cmd"}
START_LOCK = STATE_DIR / "starting.lock"


# ------------------------------------------------------------------ helpers
def say(message: str = "") -> None:
    print(message, flush=True)


def fail(message: str, hint: str = "") -> int:
    say()
    say(f"ERROR: {message}")
    if hint:
        say(hint)
    return 1


def venv_python() -> Path:
    return ROOT / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


# Local checks must never go through a proxy configured on the laptop.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run_quiet(command: list[str], timeout: float = 600, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    """Run a helper command, decoding output safely whatever the Windows code page is."""
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def get_json(url: str, timeout: float = 3) -> dict | None:
    try:
        with _LOCAL.open(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def get_ok(url: str, timeout: float = 3) -> bool:
    try:
        with _LOCAL.open(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, OSError):
        return False


def is_qtr_health(payload: dict | None) -> bool:
    """QTR's /health always reports these keys; an unrelated server does not."""
    return bool(payload) and {"components", "execution_mode", "data_mode"} <= set(payload or {})


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def process_alive(pid: int | None, title: str) -> bool:
    """True only if `pid` is still the process this launcher started.

    Windows: the process command line must contain this launcher's helper script
    (qtr_backend.cmd / qtr_frontend.cmd), so a recycled PID or any unrelated python/node/cmd
    process never matches. Window titles are only a fallback: Windows Terminal (the default
    console on Windows 11) does not report them to tasklist."""
    if not pid:
        return False
    if WINDOWS:
        helper = HELPERS[title]
        try:
            query = run_quiet(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                               f"(Get-CimInstance -ClassName Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                              timeout=30)
            if query.returncode == 0:
                return helper.lower() in query.stdout.lower()
        except (OSError, subprocess.SubprocessError):
            pass
        listing = run_quiet(["tasklist", "/V", "/FO", "CSV", "/NH", "/FI", f"PID eq {int(pid)}"], timeout=30)
        return f'"{int(pid)}"' in listing.stdout and title in listing.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # the PID must still be the process group leader we created (not a recycled PID)
        return os.getpgid(pid) == pid
    except OSError:
        return False


def start_window(title: str, command: list[str], cwd: Path, log_name: str) -> subprocess.Popen:
    """Start a long-running process in its own visible console window (Windows) or process group."""
    if WINDOWS:
        # The helper .cmd sets the window title, runs the command and keeps the window open on exit
        # so any error stays readable. `cmd /d /c ""<path>""` keeps the path quoted even when the
        # folder name contains spaces, parentheses or '&'; /d skips AutoRun commands.
        helper = ROOT / "scripts" / "windows" / HELPERS[title]
        return subprocess.Popen(f'cmd.exe /d /c ""{helper}""', cwd=cwd,
                                creationflags=subprocess.CREATE_NEW_CONSOLE)  # type: ignore[attr-defined]
    LOG_DIR.mkdir(exist_ok=True)
    log = open(LOG_DIR / log_name, "ab")  # noqa: SIM115 - handed to the child process
    return subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def stop_process(pid: int, title: str) -> bool:
    if not process_alive(pid, title):
        return False
    if WINDOWS:
        # /T stops the whole tree (window -> python/node), /F because console apps ignore a polite close.
        run_quiet(["taskkill", "/PID", str(int(pid)), "/T", "/F"], timeout=60)
        for _ in range(20):
            if not process_alive(pid, title):
                break
            time.sleep(0.5)
    else:
        os.killpg(pid, signal.SIGTERM)
        for _ in range(100):
            if not process_alive(pid, title):
                break
            time.sleep(0.1)
        else:
            os.killpg(pid, signal.SIGKILL)
    return True


def wait_until(check, timeout: float, alive=None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        if alive is not None and not alive():
            return False
        time.sleep(1)
    return check()


def safe_settings_summary() -> dict:
    """Non-secret settings only (mode flags and capital). API keys are never read into output."""
    code = ("import json,sys\nfrom pydantic import ValidationError\nfrom app.core.config import Settings\n"
            "try:\n    s=Settings()\n"
            "except ValidationError as e:\n"
            "    print(json.dumps({'errors':[(' / '.join(str(p) for p in x['loc']) or 'settings')+': '+x['msg'] "
            "for x in e.errors(include_input=False,include_url=False,include_context=False)]}));sys.exit(3)\n"
            "print(json.dumps({'trading_mode':s.trading_mode,'execution_mode':s.execution_mode,"
            "'live_trading_enabled':s.live_trading_enabled,'demo_mode':s.demo_mode,"
            "'starting_equity':s.starting_equity,'sqlite':s.database_url.startswith('sqlite'),"
            "'database_file':s.database_url.split('///',1)[1] if s.database_url.startswith('sqlite') else None}))")
    result = run_quiet([str(venv_python()), "-c", code], timeout=120)
    if result.returncode == 3:
        raise RuntimeError("; ".join(json.loads(result.stdout.strip().splitlines()[-1])["errors"]))
    if result.returncode != 0:
        # Only the exception type is shown: tracebacks can quote configuration values.
        last = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""
        raise RuntimeError(last.split(":", 1)[0] or "configuration could not be loaded")
    return json.loads(result.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------------ commands
def pid_running(pid: int) -> bool:
    """Is any process with this PID running? (Never signals it: on Windows os.kill would terminate it.)"""
    if WINDOWS:
        listing = run_quiet(["tasklist", "/FO", "CSV", "/NH", "/FI", f"PID eq {int(pid)}"], timeout=30)
        return f'"{int(pid)}"' in listing.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_start_lock() -> bool:
    """Only one RUN_QTR.bat may start QTR_Trade at a time (a quick double double-click)."""
    STATE_DIR.mkdir(exist_ok=True)
    try:
        if START_LOCK.exists():
            holder = START_LOCK.read_text(encoding="utf-8").strip()
            too_old = time.time() - START_LOCK.stat().st_mtime > 900
            if too_old or not (holder.isdigit() and pid_running(int(holder))):
                START_LOCK.unlink()  # left behind by a launcher window that was closed mid-start
        handle = os.open(START_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(handle, str(os.getpid()).encode())
    os.close(handle)
    return True


def start(open_browser: bool = True) -> int:
    os.chdir(ROOT)
    if not acquire_start_lock():
        return fail("QTR_Trade is already being started by another RUN_QTR.bat window.",
                    "Wait for that window to finish. If none is open, run RUN_QTR.bat again in 15 minutes.")
    try:
        return _start(open_browser)
    finally:
        START_LOCK.unlink(missing_ok=True)


def _start(open_browser: bool) -> int:
    say("QTR_Trade is starting...")
    say(f"Project folder: {ROOT}")

    if not (ROOT / ".env").is_file():
        return fail("The configuration file .env was not found in the project folder.",
                    "QTR_Trade needs its .env (with your existing API keys) to run.\n"
                    "Restore your .env file into the project folder and run RUN_QTR.bat again.")
    if not venv_python().is_file():
        return fail("The Python environment (.venv) was not found.", "Run RUN_QTR.bat again to create it.")
    if not (FRONTEND / "node_modules").is_dir():
        return fail("Frontend packages are not installed (frontend\\node_modules is missing).",
                    "Run RUN_QTR.bat again so it can install them.")

    try:
        config = safe_settings_summary()
    except RuntimeError as exc:
        return fail(f"The configuration in .env is not valid: {exc}")
    if config["live_trading_enabled"] or config["trading_mode"] != "paper" or config["execution_mode"] != "paper":
        return fail("This launcher only runs QTR_Trade in PAPER mode.",
                    "Set TRADING_MODE=paper, EXECUTION_MODE=paper and LIVE_TRADING_ENABLED=false in .env.")

    # 1) Database: apply pending migrations only (never resets or recreates data).
    say("Checking database migrations...")
    migrate = run_quiet([str(venv_python()), "-m", "alembic", "upgrade", "head"])
    if migrate.returncode != 0:
        say(migrate.stdout[-2000:])
        say(migrate.stderr[-3000:])
        return fail("Database migration failed. QTR_Trade was NOT started.",
                    "Nothing was deleted. Please share the error above for help.")
    current = run_quiet([str(venv_python()), "-m", "alembic", "current"])
    revision = next((line.split()[0] for line in current.stdout.splitlines() if "(head)" in line), "unknown")
    applied = [line.split("Running upgrade", 1)[1].strip() for line in migrate.stderr.splitlines() if "Running upgrade" in line]
    say(f"Database: at migration head ({revision})" + (f", applied {len(applied)} pending migration(s)" if applied else ", nothing to apply"))

    state = load_state()
    # 2) Backend (no --reload: the scheduler and paper loop run inside this single process).
    health = get_json(f"{BACKEND_URL}/health")
    if is_qtr_health(health):
        say("Backend: already running - not starting a second copy")
    elif port_open(BACKEND_PORT):
        return fail(f"Port {BACKEND_PORT} is already used by another program (not QTR_Trade).",
                    f"Close the program using port {BACKEND_PORT} and run RUN_QTR.bat again.")
    else:
        say("Starting backend... (a new 'QTR_Trade Backend' window opens; this can take up to 3 minutes)")
        backend = start_window(BACKEND_TITLE, [str(venv_python()), "-m", "uvicorn", "app.main:app", "--host", BACKEND_HOST,
                                               "--port", str(BACKEND_PORT)], ROOT, "backend.log")
        state.update({"backend_pid": backend.pid, "backend_started_at": datetime.now(UTC).isoformat()})
        save_state(state)
        ready = wait_until(lambda: is_qtr_health(get_json(f"{BACKEND_URL}/health")), 180,
                           alive=lambda: backend.poll() is None)
        if not ready:
            return fail("The backend did not become healthy.",
                        "Look at the 'QTR_Trade Backend' window for the error. The browser was NOT opened.")
        health = get_json(f"{BACKEND_URL}/health")
    overall = health.get("status") if health else "?"
    say(f"Backend: READY  ({BACKEND_URL}, overall health {overall})")
    if overall != "HEALTHY":
        say("         (normal for a few minutes after start while market data syncs; see STATUS_QTR.bat)")

    # 3) Frontend (Vite dev server; --strictPort so it never silently moves to another port).
    proxied = get_json(f"http://127.0.0.1:{FRONTEND_PORT}/health")
    if is_qtr_health(proxied):
        say("Frontend: already running - not starting a second copy")
    elif port_open(FRONTEND_PORT):
        return fail(f"Port {FRONTEND_PORT} is already used by another program (not QTR_Trade).",
                    f"Close the program using port {FRONTEND_PORT} and run RUN_QTR.bat again.")
    else:
        say("Starting frontend... (a new 'QTR_Trade Frontend' window opens)")
        frontend = start_window(FRONTEND_TITLE, ["npm", "run", "dev", "--", "--strictPort"], FRONTEND, "frontend.log")
        state.update({"frontend_pid": frontend.pid, "frontend_started_at": datetime.now(UTC).isoformat()})
        save_state(state)
        ready = wait_until(lambda: get_ok(f"http://127.0.0.1:{FRONTEND_PORT}/") and
                           is_qtr_health(get_json(f"http://127.0.0.1:{FRONTEND_PORT}/health")), 120,
                           alive=lambda: frontend.poll() is None)
        if not ready:
            return fail("The frontend did not start.",
                        "Look at the 'QTR_Trade Frontend' window for the error. The browser was NOT opened.")
    say(f"Frontend: READY  ({FRONTEND_URL})")

    # 4) Browser, only once both sides are confirmed ready.
    if open_browser:
        try:
            opened = webbrowser.open(FRONTEND_URL)
        except webbrowser.Error:
            opened = False
        say(f"Browser: OPENED  ({FRONTEND_URL})" if opened else
            f"Browser: could not be opened automatically - open {FRONTEND_URL} yourself")
    else:
        say(f"Browser: not opened (--no-browser). Open {FRONTEND_URL}")
    summary(config)
    return 0


def summary(config: dict | None = None) -> None:
    account = get_json(f"{BACKEND_URL}/api/v1/paper/account") or {}
    say()
    say("==============================================")
    say(f" Paper trading mode: {'ENABLED' if account.get('execution_mode') == 'PAPER' else 'UNKNOWN'}")
    say(f" Live trading:       {account.get('live_trading', 'DISABLED')}")
    if account.get("initial_balance"):
        say(f" Paper capital:      ${float(account['initial_balance']):,.0f}"
            f"  (equity now ${float(account.get('equity', 0)):,.2f})")
    if config and config.get("database_file"):
        say(f" Database:           {(ROOT / config['database_file']).resolve()}")
    say(f" Open QTR_Trade at:  {FRONTEND_URL}")
    say("==============================================")
    say("QTR_Trade keeps running in its two windows (Backend, Frontend).")
    say("Closing the browser does NOT stop it. Use STOP_QTR.bat to stop it.")


def stop() -> int:
    os.chdir(ROOT)
    state = load_state()
    stopped, remaining = [], {}
    for key, title in (("frontend_pid", FRONTEND_TITLE), ("backend_pid", BACKEND_TITLE)):
        pid = state.get(key)
        if not pid:
            continue
        if stop_process(int(pid), title):
            stopped.append(title)
        if process_alive(int(pid), title):  # could not be stopped: keep tracking it
            remaining[key] = pid
    if remaining:
        save_state(remaining)
    elif STATE_FILE.exists():
        STATE_FILE.unlink()
    for name, url in (("Backend", f"{BACKEND_URL}/health"), ("Frontend", f"http://127.0.0.1:{FRONTEND_PORT}/health")):
        if wait_until(lambda url=url: not is_qtr_health(get_json(url, timeout=1)), 15):
            continue
        say(f"NOTE: a QTR_Trade {name} is still running. It was not started by this RUN_QTR.bat or could")
        say("      not be stopped, so it was left alone. Close its window yourself to stop it.")
    say("Stopped: " + (", ".join(stopped) if stopped else "nothing was running from RUN_QTR.bat"))
    say("Database, logs and configuration were not touched.")
    say("QTR_Trade stopped.")
    return 0


def status() -> int:
    os.chdir(ROOT)
    state = load_state()
    backend = get_json(f"{BACKEND_URL}/health") or {}
    frontend = is_qtr_health(get_json(f"http://127.0.0.1:{FRONTEND_PORT}/health"))
    say("QTR_Trade status")
    say(f" Backend process (launcher):  {'running' if process_alive(state.get('backend_pid'), BACKEND_TITLE) else 'not running'}")
    say(f" Frontend process (launcher): {'running' if process_alive(state.get('frontend_pid'), FRONTEND_TITLE) else 'not running'}")
    say(f" Backend health:  {backend.get('status') if is_qtr_health(backend) else 'NOT REACHABLE'}  ({BACKEND_URL}/health)")
    if is_qtr_health(backend):
        for name in ("binance", "gemini", "market_data", "scheduler"):
            say(f"   {name:<12} {backend['components'].get(name, {}).get('state', '?')}")
    say(f" Frontend:        {'READY' if frontend else 'NOT REACHABLE'}  ({FRONTEND_URL})")
    if is_qtr_health(backend):
        summary()
    return 0


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):  # never crash on characters the console code page lacks
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    command = argv[1] if len(argv) > 1 else "start"
    if command == "start":
        return start(open_browser="--no-browser" not in argv)
    if command == "stop":
        return stop()
    if command == "status":
        return status()
    say("Usage: qtr_launcher.py start [--no-browser] | stop | status")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
