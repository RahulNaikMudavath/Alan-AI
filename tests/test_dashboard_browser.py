"""Opt-in browser regression checks for the dashboard.

Skipped by default so the normal suite stays fast and deterministic. Enable it
explicitly when a Chromium binary is available:

    $env:RECALLKIT_BROWSER_TESTS="1"
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_dashboard_browser.py -q

Everything runs against a temporary database and a temporary report directory.
The real ``data/recallkit.db`` is never opened.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

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

# Local sockets are required to probe the app and the DevTools endpoint.
pytestmark = [
    pytest.mark.allow_network,
    pytest.mark.skipif(
        os.environ.get("RECALLKIT_BROWSER_TESTS") != "1",
        reason="set RECALLKIT_BROWSER_TESTS=1 to run browser checks",
    ),
]


def _find_browser() -> str:
    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("msedge") or shutil.which("chrome") or shutil.which("chromium")
    if found:
        return found
    pytest.skip("no Chromium-based browser found")


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with closing(socket.create_connection(("127.0.0.1", port), timeout=0.5)):
                return
        except OSError:
            time.sleep(0.3)
    raise AssertionError(f"port {port} did not open in time")


@pytest.fixture
def dashboard_env(tmp_path: Path) -> Iterator[dict]:
    """Start a temporary app plus a headless browser and yield the endpoints."""
    from app.cli import main
    from app.config import Settings
    from app.db import Database

    database_path = tmp_path / "browser.db"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()

    # Copy the generated reports so the Evaluation Lab has real content to rank.
    # They are read-only inputs; the temporary directory is what gets written to.
    source_reports = PROJECT_ROOT / "eval" / "reports"
    copied = 0
    if source_reports.is_dir():
        for report in sorted(source_reports.glob("*.json")):
            shutil.copyfile(report, reports_dir / report.name)
            copied += 1
    if copied == 0:
        pytest.skip("no evaluation reports available to copy into the temp directory")

    settings = Settings(
        _env_file=None,
        database_path=database_path,
        eval_reports_path=reports_dir,
        environment="test",
    )
    Database(database_path).initialize()
    assert main(["seed-demo"], settings=settings) == 0

    app_port = _free_port()
    cdp_port = _free_port()
    env = {**os.environ, "RECALLKIT_DATABASE_PATH": str(database_path)}

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
        env={**env, "RECALLKIT_EVAL_REPORTS_PATH": str(reports_dir)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    browser = None
    try:
        _wait_for_port(app_port)
        profile = tmp_path / "browser-profile"
        browser = subprocess.Popen(
            [
                _find_browser(),
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
        _wait_for_port(cdp_port)
        yield {"base": f"http://127.0.0.1:{app_port}", "cdp_port": cdp_port}
    finally:
        for process in (browser, server):
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()


def test_dashboard_browser_regressions(dashboard_env: dict) -> None:
    script = PROJECT_ROOT / "scripts" / "dashboard_browser_check.mjs"
    result = subprocess.run(
        [
            "node",
            str(script),
            "--cdp-port",
            str(dashboard_env["cdp_port"]),
            "--base",
            dashboard_env["base"],
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode == 2:
        pytest.fail(f"browser check could not run: {result.stderr or result.stdout}")

    summary = json.loads(result.stdout)
    failures = [
        check
        for check in summary["results"]
        if check.get("overflow") or check.get("passed") is False
    ]

    assert failures == [], f"browser regressions: {failures}"
    assert summary["consoleErrors"] == [], f"console errors: {summary['consoleErrors']}"
    assert result.returncode == 0
