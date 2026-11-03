# Authenticated Load Tester

[![tests](https://github.com/paramdl/authenticated-load-tester/actions/workflows/tests.yml/badge.svg)](https://github.com/paramdl/authenticated-load-tester/actions/workflows/tests.yml)

A lightweight load tester for pages that sit **behind a login**. Each virtual user signs in for real
(session cookie or Bearer token), then hits every configured page N times while the tool records
time-to-first-byte, full response time, status codes and error rate. Results land as HTML, CSV and JSON,
and an optional error-rate gate makes the run fail a CI job.

Built with Python `asyncio` + `aiohttp`, configured with one YAML file per platform.

## Try it in 30 seconds (no external site needed)
```bash
pip install -r requirements.txt
python demo/demo_app.py                                   # local app with login, a slow page and a flaky page
python load_tester.py --config configs/demo_cookie.yaml   # in a second terminal
```
Open the HTML report in `reports/`. The demo's `/reports` page is deliberately slow and `/flaky` fails 1 in 5
requests, so you can see how both show up.

> Only load-test systems you own or have written permission to test. The bundled demo app exists so you never need to point this at someone else's site.

## Features
| | |
|---|---|
| **Real logins** | `cookie` (POST credentials, keep the session), `bearer` (POST, read a token from a JSON path), `session_cookie` / `token` (paste one in) |
| **Concurrency** | N users in parallel (`--concurrency`), each with its own session |
| **Metrics** | TTFB, response time avg / p95 (nearest-rank) / max, error rate per page |
| **Reports** | HTML summary, CSV of every request, JSON for further analysis |
| **CI gate** | `--max-error-rate 2` exits 1 when more than 2% of requests fail |

## Usage
```bash
python load_tester.py --config configs/myapp.yaml \
  --iterations 5 --concurrency 10 --delay 0.5 \
  --output reports/run1 --max-error-rate 2
```
Start from [`configs/generic_template.yaml`](configs/generic_template.yaml); every option is commented there.

## What counts as a failure
Transport errors, timeouts and **any 4xx or 5xx**. A 401/403 in the middle of a run usually means sessions are
expiring under load, so it must not be counted as a success. Failed requests are excluded from the timing
stats so a fast error page can't make the average look better.

## Tests
```bash
pip install pytest && pytest -q
```
Unit tests cover the percentile maths and failure rules; end-to-end tests start the demo app and run the CLI
for both login types and both outcomes of the CI gate. The same suite runs in GitHub Actions on every push.
