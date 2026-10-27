"""Automated script to capture genuine screenshots of RecallKit for documentation.

Sets up a temporary database, seeds it with demo data, launches a headless
browser with CDP, and captures desktop screenshots to docs/screenshots/.
Never touches data/recallkit.db.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from app.config import PROJECT_ROOT

BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_browser() -> str:
    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("msedge") or shutil.which("chrome") or shutil.which("chromium")
    if found:
        return found
    raise RuntimeError("No Chromium-based browser found on the system.")


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with closing(socket.create_connection(("127.0.0.1", port), timeout=0.5)):
                return
        except OSError:
            time.sleep(0.3)
    raise TimeoutError(f"Port {port} did not open in time.")


@contextmanager
def temporary_demo_env() -> Iterator[dict]:
    from app.cli import main as cli_main
    from app.config import Settings
    from app.db import Database

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        database_path = tmp_path / "screenshot-demo.db"
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()

        # Copy existing evaluation reports for the Evaluation Lab view
        source_reports = PROJECT_ROOT / "eval" / "reports"
        if source_reports.is_dir():
            for report in sorted(source_reports.glob("*.json")):
                shutil.copyfile(report, reports_dir / report.name)

        settings = Settings(
            _env_file=None,
            database_path=database_path,
            eval_reports_path=reports_dir,
            environment="demo",
        )
        Database(database_path).initialize()
        assert cli_main(["seed-demo", "--user-id", "demo-user"], settings=settings) == 0

        app_port = free_port()
        cdp_port = free_port()
        env = {
            **os.environ,
            "RECALLKIT_DATABASE_PATH": str(database_path),
            "RECALLKIT_EVAL_REPORTS_PATH": str(reports_dir),
        }

        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--port",
                str(app_port),
                "--log-level",
                "warning",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        browser = None
        try:
            wait_for_port(app_port)
            profile = tmp_path / "browser-profile"
            browser = subprocess.Popen(
                [
                    find_browser(),
                    "--headless=new",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    "--no-first-run",
                    "--no-default-browser-check",
                    f"--remote-debugging-port={cdp_port}",
                    "--remote-allow-origins=*",
                    f"--user-data-dir={profile}",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            wait_for_port(cdp_port)
            yield {
                "base": f"http://127.0.0.1:{app_port}",
                "cdp_port": cdp_port,
                "tmp_dir": tmp_dir,
            }
        finally:
            for process in (browser, server):
                if process is not None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()


def main() -> int:
    print("Preparing temporary environment for screenshot capture...")
    docs_dir = PROJECT_ROOT / "docs" / "screenshots"
    docs_dir.mkdir(parents=True, exist_ok=True)

    with temporary_demo_env() as env_info:
        node_script = PROJECT_ROOT / "scripts" / "capture_screenshots.mjs"
        print(f"Connecting to browser on CDP port {env_info['cdp_port']}...")
        result = subprocess.run(
            [
                "node",
                str(node_script),
                "--cdp-port",
                str(env_info["cdp_port"]),
                "--base",
                env_info["base"],
                "--out-dir",
                str(docs_dir),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        print(result.stdout)
        if result.returncode != 0:
            print("Screenshot capture script failed:", result.stderr, file=sys.stderr)
            return result.returncode

    # Verify screenshots exist
    expected_files = ["overview.png", "explorer.png", "debugger.png", "evaluation.png"]
    missing = [f for f in expected_files if not (docs_dir / f).exists()]
    if missing:
        print(f"Error: Missing screenshots: {missing}", file=sys.stderr)
        return 1

    print("Successfully captured all 4 screenshots into docs/screenshots/:")
    for f in expected_files:
        p = docs_dir / f
        print(f"  - {p} ({p.stat().st_size:,} bytes)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
