"""
Authenticated Load Tester
Reusable load testing tool for any web platform with login-based auth.
Supports session cookies, Bearer tokens, and custom header auth.
"""

import asyncio
import aiohttp
import yaml
import json
import time
import csv
import argparse
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


# ──────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────

@dataclass
class RequestResult:
    user: str
    page: str
    url: str
    status: int
    ttfb_ms: float          # time to first byte
    total_ms: float         # full response time
    content_length: int
    error: Optional[str] = None
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


@dataclass
class PageStats:
    page: str
    url: str
    total_requests: int = 0
    success: int = 0
    failures: int = 0
    ttfb_values: list = field(default_factory=list)
    total_values: list = field(default_factory=list)

    @property
    def error_rate(self):
        return round(self.failures / self.total_requests * 100, 1) if self.total_requests else 0

    def summary(self):
        tv = self.total_values
        tb = self.ttfb_values
        return {
            "page": self.page,
            "url": self.url,
            "requests": self.total_requests,
            "success": self.success,
            "failures": self.failures,
            "error_rate_%": self.error_rate,
            "ttfb_avg_ms": round(statistics.mean(tb), 1) if tb else None,
            "ttfb_p95_ms": percentile(tb, 95),
            "ttfb_max_ms": round(max(tb), 1) if tb else None,
            "response_avg_ms": round(statistics.mean(tv), 1) if tv else None,
            "response_p95_ms": percentile(tv, 95),
            "response_max_ms": round(max(tv), 1) if tv else None,
        }


def percentile(values: list, pct: float) -> Optional[float]:
    """Nearest-rank percentile: the smallest value with at least pct% of samples at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return round(ordered[rank - 1], 1)


def is_failure(result: "RequestResult") -> bool:
    """Transport errors and any 4xx/5xx count as failures; a 401/403 mid-test usually means the session died."""
    return bool(result.error) or result.status == 0 or result.status >= 400


# ──────────────────────────────────────────────
# Config loader
# ──────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ──────────────────────────────────────────────
# Auth handlers
# ──────────────────────────────────────────────

async def authenticate_cookie(session: aiohttp.ClientSession, auth_cfg: dict, user: dict) -> bool:
    """POST credentials, store session cookies automatically."""
    url = auth_cfg["login_url"]
    payload_template = auth_cfg["payload"]
    payload = {k: user.get(v.lstrip("$"), v) if v.startswith("$") else v
               for k, v in payload_template.items()}
    headers = auth_cfg.get("headers", {})

    try:
        async with session.post(url, json=payload, headers=headers, allow_redirects=True) as resp:
            if resp.status in auth_cfg.get("success_status", [200, 201, 302]):
                return True
            body = await resp.text()
            # Check for success token in body if specified
            success_key = auth_cfg.get("success_body_contains")
            if success_key and success_key in body:
                return True
            reason = body[:120].replace('\n', ' ') if body else "no body"
            print(f"    [WARN] Auth failed for {user['username']} - HTTP {resp.status}: {reason}")
            return False
    except Exception as e:
        print(f"    [ERROR] Auth exception for {user['username']}: {e}")
        return False


def authenticate_session_cookie(session: aiohttp.ClientSession, auth_cfg: dict, user: dict) -> bool:
    """Inject a pre-copied session cookie string directly (no login POST needed).
    The user copies the Cookie header value from browser DevTools and pastes it
    into the config under auth.session_cookie or per-user as user['session_cookie'].
    """
    cookie_str = user.get("session_cookie") or auth_cfg.get("session_cookie", "")
    if not cookie_str:
        print(f"    [WARN] No session_cookie set for {user['username']}")
        return False
    domain = auth_cfg.get("cookie_domain", "")
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if "=" in pair:
            name, _, value = pair.partition("=")
            session.cookie_jar.update_cookies({name.strip(): value.strip()})
    return True


async def authenticate_bearer(session: aiohttp.ClientSession, auth_cfg: dict, user: dict) -> Optional[str]:
    """POST credentials, extract Bearer token from response."""
    url = auth_cfg["login_url"]
    payload_template = auth_cfg["payload"]
    payload = {k: user.get(v.lstrip("$"), v) if v.startswith("$") else v
               for k, v in payload_template.items()}
    headers = auth_cfg.get("headers", {})

    try:
        async with session.post(url, json=payload, headers=headers) as resp:
            data = await resp.json()
            token_path = auth_cfg.get("token_path", "token")
            token = data
            for key in token_path.split("."):
                token = token.get(key, {}) if isinstance(token, dict) else None
            return str(token) if token else None
    except Exception as e:
        print(f"    [ERROR] Bearer auth exception for {user['username']}: {e}")
        return None


# ──────────────────────────────────────────────
# Page requester
# ──────────────────────────────────────────────

async def fetch_page(
    session: aiohttp.ClientSession,
    user: dict,
    page: dict,
    base_url: str,
    extra_headers: dict = None,
) -> RequestResult:
    url = page.get("url") or (base_url.rstrip("/") + "/" + page["path"].lstrip("/"))
    name = page.get("name", page.get("path", url))
    headers = {**(extra_headers or {}), **page.get("headers", {})}

    t_start = time.perf_counter()
    ttfb = None
    status = 0
    length = 0
    error = None

    try:
        async with session.get(url, headers=headers) as resp:
            status = resp.status
            # Read first chunk = TTFB
            content = b""
            async for chunk in resp.content.iter_chunked(1024):
                if ttfb is None:
                    ttfb = (time.perf_counter() - t_start) * 1000
                content += chunk
            length = len(content)
    except aiohttp.ClientError as e:
        error = str(e)
        status = 0
    except asyncio.TimeoutError:
        error = "timeout"
        status = 0

    total = (time.perf_counter() - t_start) * 1000
    ttfb = ttfb or total

    return RequestResult(
        user=user["username"],
        page=name,
        url=url,
        status=status,
        ttfb_ms=round(ttfb, 2),
        total_ms=round(total, 2),
        content_length=length,
        error=error,
    )


# ──────────────────────────────────────────────
# Single user worker
# ──────────────────────────────────────────────

async def run_user_session(
    user: dict,
    config: dict,
    iterations: int,
    delay_between_pages: float,
    results: list,
    semaphore: asyncio.Semaphore,
):
    auth_cfg = config["auth"]
    base_url = config["base_url"]
    pages = config["pages"]
    timeout = aiohttp.ClientTimeout(total=config.get("request_timeout", 30))
    connector = aiohttp.TCPConnector(ssl=config.get("verify_ssl", True))

    async with semaphore:
        # unsafe=True: keep cookies from IP-address hosts (staging boxes, localhost); aiohttp drops them by default.
        async with aiohttp.ClientSession(connector=connector, timeout=timeout,
                                         cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
            # Authenticate
            extra_headers = {}
            auth_type = auth_cfg.get("type", "cookie")

            if auth_type == "cookie":
                ok = await authenticate_cookie(session, auth_cfg, user)
                if not ok:
                    print(f"  [SKIP] Could not authenticate {user['username']}")
                    return
            elif auth_type == "bearer":
                token = await authenticate_bearer(session, auth_cfg, user)
                if not token:
                    print(f"  [SKIP] No token for {user['username']}")
                    return
                extra_headers["Authorization"] = f"Bearer {token}"
            elif auth_type == "session_cookie":
                ok = authenticate_session_cookie(session, auth_cfg, user)
                if not ok:
                    print(f"  [SKIP] No session cookie for {user['username']}")
                    return
            elif auth_type == "token":
                # Pre-obtained token pasted directly — no login POST needed
                token_val = user.get("token") or auth_cfg.get("token", "")
                token_prefix = auth_cfg.get("token_prefix", "Bearer")
                if not token_val:
                    print(f"  [SKIP] No token set for {user['username']}")
                    return
                header_name = auth_cfg.get("token_header", "Authorization")
                extra_headers[header_name] = f"{token_prefix} {token_val}".strip()
            else:
                print(f"  [WARN] Unknown auth type: {auth_type}")
                return

            print(f"  [OK] Authenticated: {user['username']}")

            # Hit each page N times
            for i in range(iterations):
                for page in pages:
                    result = await fetch_page(session, user, page, base_url, extra_headers)
                    results.append(result)
                    status_icon = "[FAIL]" if is_failure(result) else "[OK]"
                    print(f"      {status_icon} [{user['username']}] {result.page} "
                          f"- {result.status} | TTFB: {result.ttfb_ms}ms | Total: {result.total_ms}ms")
                    if delay_between_pages > 0:
                        await asyncio.sleep(delay_between_pages)


# ──────────────────────────────────────────────
# Report generators
# ──────────────────────────────────────────────

def build_page_stats(results: list, config: dict) -> dict:
    stats = {}
    for r in results:
        if r.page not in stats:
            stats[r.page] = PageStats(page=r.page, url=r.url)
        ps = stats[r.page]
        ps.total_requests += 1
        if is_failure(r):
            ps.failures += 1
        else:
            ps.success += 1
            ps.ttfb_values.append(r.ttfb_ms)
            ps.total_values.append(r.total_ms)
    return stats


def save_csv(results: list, out_path: Path):
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp","user","page","url","status","ttfb_ms","total_ms","content_length","error"])
        w.writeheader()
        for r in results:
            w.writerow(vars(r))
    print(f"\n  CSV saved -> {out_path}")


def save_json(results: list, stats: dict, config: dict, meta: dict, out_path: Path):
    payload = {
        "meta": meta,
        "platform": config.get("name", "unknown"),
        "summary": [ps.summary() for ps in stats.values()],
        "raw_results": [vars(r) for r in results],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"  JSON saved -> {out_path}")


def save_html(stats: dict, config: dict, meta: dict, out_path: Path):
    rows = ""
    for ps in stats.values():
        s = ps.summary()
        er_color = "#c0392b" if s["error_rate_%"] > 0 else "#27ae60"
        ttfb_color = "#c0392b" if (s["ttfb_avg_ms"] or 0) > 1000 else \
                     "#e67e22" if (s["ttfb_avg_ms"] or 0) > 600 else "#27ae60"
        rows += f"""
        <tr>
          <td>{s['page']}</td>
          <td><a href="{s['url']}" target="_blank">{s['url']}</a></td>
          <td>{s['requests']}</td>
          <td style="color:{er_color}">{s['error_rate_%']}%</td>
          <td style="color:{ttfb_color}">{s['ttfb_avg_ms']} ms</td>
          <td>{s['ttfb_p95_ms']} ms</td>
          <td>{s['ttfb_max_ms']} ms</td>
          <td>{s['response_avg_ms']} ms</td>
          <td>{s['response_p95_ms']} ms</td>
          <td>{s['response_max_ms']} ms</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Load Test Report — {config.get('name','Platform')}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#f4f6f8; margin:0; padding:20px; color:#2c3e50; }}
  h1 {{ color:#2c3e50; }} h2 {{ color:#34495e; border-bottom:2px solid #3498db; padding-bottom:6px; }}
  .meta {{ background:#fff; padding:16px 20px; border-radius:8px; margin-bottom:24px; box-shadow:0 1px 4px rgba(0,0,0,.08); display:flex; gap:32px; flex-wrap:wrap; }}
  .meta div {{ display:flex; flex-direction:column; }} .meta span {{ font-size:11px; color:#7f8c8d; text-transform:uppercase; letter-spacing:.5px; }}
  .meta strong {{ font-size:18px; margin-top:4px; }}
  table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:8px; overflow:hidden; box-shadow:0 1px 4px rgba(0,0,0,.08); }}
  th {{ background:#2c3e50; color:#fff; text-align:left; padding:10px 14px; font-size:12px; text-transform:uppercase; letter-spacing:.5px; }}
  td {{ padding:10px 14px; border-bottom:1px solid #ecf0f1; font-size:13px; }}
  tr:last-child td {{ border-bottom:none; }}
  tr:hover td {{ background:#f8f9fa; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:12px; font-size:11px; font-weight:600; }}
  .good {{ background:#d5f5e3; color:#1e8449; }} .warn {{ background:#fef9e7; color:#b7950b; }} .bad {{ background:#fadbd8; color:#922b21; }}
  footer {{ text-align:center; color:#95a5a6; font-size:12px; margin-top:24px; }}
</style>
</head>
<body>
<h1>🚀 Load Test Report</h1>
<h2>Platform: {config.get('name', 'Unknown')}</h2>
<div class="meta">
  <div><span>Base URL</span><strong>{config.get('base_url','')}</strong></div>
  <div><span>Users</span><strong>{meta['users']}</strong></div>
  <div><span>Iterations / User</span><strong>{meta['iterations']}</strong></div>
  <div><span>Concurrency</span><strong>{meta['concurrency']}</strong></div>
  <div><span>Total Requests</span><strong>{meta['total_requests']}</strong></div>
  <div><span>Duration</span><strong>{meta['duration_s']}s</strong></div>
  <div><span>Run At</span><strong>{meta['started_at']}</strong></div>
</div>

<h2>Page-by-Page Results</h2>
<table>
  <thead>
    <tr>
      <th>Page</th><th>URL</th><th>Requests</th><th>Error Rate</th>
      <th>TTFB Avg</th><th>TTFB p95</th><th>TTFB Max</th>
      <th>Response Avg</th><th>Response p95</th><th>Response Max</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>

<h2>Thresholds Legend</h2>
<p>
  <span class="badge good">TTFB &lt; 600ms — Good</span>&nbsp;
  <span class="badge warn">TTFB 600–1000ms — Needs improvement</span>&nbsp;
  <span class="badge bad">TTFB &gt; 1000ms — Critical</span>
</p>

<footer>Generated by Authenticated Load Tester &bull; {meta['started_at']}</footer>
</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML saved -> {out_path}")


def print_summary(stats: dict, meta: dict):
    print("\n" + "="*65)
    print(f"  LOAD TEST COMPLETE - {meta['platform']}")
    print("="*65)
    print(f"  Users: {meta['users']}  |  Iterations: {meta['iterations']}  "
          f"|  Concurrency: {meta['concurrency']}")
    print(f"  Total Requests: {meta['total_requests']}  |  Duration: {meta['duration_s']}s")
    print("-"*65)
    header = f"{'Page':<22} {'Req':>5} {'Err%':>6} {'TTFB avg':>10} {'TTFB p95':>10} {'Resp avg':>10}"
    print(header)
    print("-"*65)
    for ps in stats.values():
        s = ps.summary()
        ttfb = f"{s['ttfb_avg_ms']}ms" if s['ttfb_avg_ms'] else "N/A"
        p95  = f"{s['ttfb_p95_ms']}ms" if s['ttfb_p95_ms'] else "N/A"
        resp = f"{s['response_avg_ms']}ms" if s['response_avg_ms'] else "N/A"
        flag = " [SLOW]" if (s['ttfb_avg_ms'] or 0) > 1000 else ("  [ERR]" if s['error_rate_%'] > 5 else "")
        print(f"  {s['page']:<20} {s['requests']:>5} {s['error_rate_%']:>5}% "
              f"{ttfb:>10} {p95:>10} {resp:>10}{flag}")
    print("="*65)


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

async def main(args):
    config = load_config(args.config)
    users = config.get("users", [])
    iterations = args.iterations or config.get("default_iterations", 3)
    concurrency = args.concurrency or config.get("default_concurrency", 5)
    delay = args.delay if args.delay is not None else config.get("delay_between_pages", 0.5)

    if not users:
        print("[ERROR] No users defined in config.")
        sys.exit(1)

    platform_name = config.get("name", "platform")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = Path(args.output or "reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    base_name = report_dir / f"{platform_name}_{ts}"

    print(f"\n{'='*65}")
    print(f"  Authenticated Load Tester - {platform_name}")
    print(f"{'='*65}")
    print(f"  Users: {len(users)}  |  Iterations: {iterations}  |  Concurrency: {concurrency}")
    print(f"  Pages: {len(config['pages'])}")
    print(f"  Output: {report_dir}/\n")

    results = []
    semaphore = asyncio.Semaphore(concurrency)
    started = time.perf_counter()
    started_at = datetime.now().isoformat()

    tasks = [
        run_user_session(user, config, iterations, delay, results, semaphore)
        for user in users
    ]
    await asyncio.gather(*tasks)

    duration = round(time.perf_counter() - started, 2)
    stats = build_page_stats(results, config)

    meta = {
        "platform": platform_name,
        "started_at": started_at,
        "duration_s": duration,
        "users": len(users),
        "iterations": iterations,
        "concurrency": concurrency,
        "total_requests": len(results),
    }

    print_summary(stats, meta)

    # Save reports
    print("\n  Saving reports...")
    save_csv(results, Path(f"{base_name}.csv"))
    save_json(results, stats, config, meta, Path(f"{base_name}.json"))
    save_html(stats, config, meta, Path(f"{base_name}.html"))

    # Optional quality gate so the run can fail a CI job.
    if args.max_error_rate is not None:
        total = sum(s.total_requests for s in stats.values())
        failed = sum(s.failures for s in stats.values())
        rate = round(failed / total * 100, 1) if total else 100.0
        if rate > args.max_error_rate:
            print(f"\n  [GATE] Error rate {rate}% exceeds --max-error-rate {args.max_error_rate}%")
            return 1
        print(f"\n  [GATE] Error rate {rate}% within --max-error-rate {args.max_error_rate}%")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Authenticated Load Tester")
    parser.add_argument("--config",      required=True, help="Path to platform YAML config")
    parser.add_argument("--iterations",  type=int,      help="Requests per user per page (overrides config)")
    parser.add_argument("--concurrency", type=int,      help="Max parallel users (overrides config)")
    parser.add_argument("--delay",       type=float,    help="Seconds between page hits per user")
    parser.add_argument("--output",                     help="Report output directory (default: ./reports)")
    parser.add_argument("--max-error-rate", type=float, help="Exit 1 if the overall error rate (%%) is above this")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args)))
