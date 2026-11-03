"""Runs the real CLI against the bundled demo app, for both login types and the CI quality gate."""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def demo_app():
    proc = subprocess.Popen([sys.executable, str(ROOT / "demo" / "demo_app.py")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            urllib.request.urlopen("http://127.0.0.1:8080/dashboard", timeout=1)
        except urllib.error.HTTPError:      # 401 means the server is up
            break
        except OSError:
            time.sleep(0.2)
    yield
    proc.terminate()
    proc.wait(timeout=10)


def run(config, tmp_path, *extra):
    cmd = [sys.executable, str(ROOT / "load_tester.py"), "--config", str(ROOT / "configs" / config),
           "--output", str(tmp_path), *extra]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def report(tmp_path):
    data = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    return {p["page"]: p for p in data["page_stats"]} if "page_stats" in data else data


def test_cookie_login_hits_every_page(demo_app, tmp_path):
    proc = run("demo_cookie.yaml", tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[SKIP]" not in proc.stdout, "a user failed to log in"
    assert len(list(tmp_path.glob("*.html"))) == 1
    assert len(list(tmp_path.glob("*.csv"))) == 1
    text = next(tmp_path.glob("*.json")).read_text(encoding="utf-8")
    assert "Dashboard" in text and "Flaky API" in text


def test_bearer_login_works(demo_app, tmp_path):
    proc = run("demo_bearer.yaml", tmp_path, "--iterations", "1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[SKIP]" not in proc.stdout


def test_gate_fails_when_error_rate_too_high(demo_app, tmp_path):
    # /flaky fails 1 in 5 requests -> ~5% overall, above a 1% budget
    proc = run("demo_cookie.yaml", tmp_path, "--max-error-rate", "1")
    assert proc.returncode == 1, proc.stdout
    assert "[GATE]" in proc.stdout


def test_gate_passes_within_budget(demo_app, tmp_path):
    proc = run("demo_cookie.yaml", tmp_path, "--max-error-rate", "25")
    assert proc.returncode == 0, proc.stdout
