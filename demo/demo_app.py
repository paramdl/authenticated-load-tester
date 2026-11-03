"""
Tiny local app to point the load tester at, so nobody load-tests a site they don't own.

  python demo/demo_app.py            # serves http://127.0.0.1:8080

Login:   POST /api/login  {"email", "password"}   -> sets a session cookie
         POST /api/token  {"username", "password"} -> {"data": {"token": "..."}}
Pages:   /dashboard, /profile (fast), /reports (deliberately slow), /flaky (every 5th request -> 500)
All pages return 401 without a valid session cookie or Bearer token.
"""
import asyncio
import itertools
import secrets

from aiohttp import web

USERS = {f"demo{i}@example.com": "demo-pass" for i in range(1, 6)}
SESSIONS: set[str] = set()
TOKENS: set[str] = set()
_flaky_counter = itertools.count(1)


def _authorised(request: web.Request) -> bool:
    if request.cookies.get("session") in SESSIONS:
        return True
    auth = request.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth[7:] in TOKENS


async def login(request: web.Request) -> web.Response:
    body = await request.json()
    if USERS.get(body.get("email")) != body.get("password"):
        return web.json_response({"error": "invalid credentials"}, status=401)
    sid = secrets.token_hex(16)
    SESSIONS.add(sid)
    resp = web.json_response({"ok": True})
    resp.set_cookie("session", sid, httponly=True)
    return resp


async def token(request: web.Request) -> web.Response:
    body = await request.json()
    if USERS.get(body.get("username")) != body.get("password"):
        return web.json_response({"error": "invalid credentials"}, status=401)
    tok = secrets.token_hex(16)
    TOKENS.add(tok)
    return web.json_response({"data": {"token": tok}})


def page(name: str, delay: float = 0.0):
    async def handler(request: web.Request) -> web.Response:
        if not _authorised(request):
            return web.json_response({"error": "unauthorised"}, status=401)
        await asyncio.sleep(delay)
        return web.Response(text=f"<html><body><h1>{name}</h1>{'<p>row</p>' * 200}</body></html>",
                            content_type="text/html")
    return handler


async def flaky(request: web.Request) -> web.Response:
    if not _authorised(request):
        return web.json_response({"error": "unauthorised"}, status=401)
    if next(_flaky_counter) % 5 == 0:
        return web.json_response({"error": "simulated outage"}, status=500)
    return web.json_response({"items": list(range(20))})


def make_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.post("/api/login", login),
        web.post("/api/token", token),
        web.get("/dashboard", page("Dashboard", 0.02)),
        web.get("/profile", page("Profile", 0.01)),
        web.get("/reports", page("Reports", 0.25)),
        web.get("/flaky", flaky),
    ])
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="127.0.0.1", port=8080)
