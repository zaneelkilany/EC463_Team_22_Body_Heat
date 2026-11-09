"""Local web app served by the hub (Subsystem F): sign-in, supervisor roster,
alert strip, athlete detail, session/consent/export, athlete view, presenter
controls. Standard library only; live updates by Server-Sent Events that push
only changed tiles. Runs fully offline.

Run:  python -m body_heat_demo.server [--port 8463] [--no-autoplay]
Then open http://localhost:8463 and sign in (accounts listed on the page).
"""

import argparse
import json
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .auth import AccessDenied
from .engine import build_demo
from . import views

STATIC = Path(__file__).parent / "static"
STATE = {}  # store, hub, accounts, roster, engine


def _user_from(handler):
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "bh_session":
            return STATE["accounts"].user_for(v), v
    return None, None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BodyHeatHub/0.1"

    def log_message(self, fmt, *args):   # quiet; the demo log lives in the UI
        pass

    # -- helpers ---------------------------------------------------------------
    def _json(self, obj, status=HTTPStatus.OK, headers=()):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _file(self, name, ctype):
        p = STATIC / name
        if not p.is_file():
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        data = p.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _member(self, device_id):
        return next((m for m in STATE["roster"] if m["device_id"] == device_id), None)

    # -- routing -----------------------------------------------------------------
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path, q = url.path, urllib.parse.parse_qs(url.query)
        user, _ = _user_from(self)
        hub, eng, acc, store = STATE["hub"], STATE["engine"], STATE["accounts"], STATE["store"]
        t = eng.t
        try:
            if path == "/":
                return self._file("app.html", "text/html; charset=utf-8")
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                ctype = {"css": "text/css", "js": "application/javascript", "svg": "image/svg+xml"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
                return self._file(name, ctype)
            if path == "/api/me":
                return self._json({"user": user, "accounts": acc.list_accounts() if not user else None,
                                   "strings": views.UI_STRINGS, "t": t,
                                   "states": [{"key": k, "label": l, "icon": i, "meaning": views.STATE_MEANING[k],
                                               "staff": views.STAFF_GUIDANCE[k], "wearer": views.WEARER_GUIDANCE[k]}
                                              for k, l, i in views.STATES]})
            if path == "/api/roster":
                acc.require_staff(user, t, "GET /api/roster")
                return self._json({"tiles": views.build_roster(hub, STATE["roster"], t),
                                   "strip": views.build_alert_strip(hub, STATE["roster"], t),
                                   "notices": views.build_notices(hub, STATE["roster"], t),
                                   "status": views.build_status(hub, t, store)})
            if path.startswith("/api/wearer/"):
                dev = path.rsplit("/", 1)[-1]
                acc.require_device_access(user, dev, t, f"GET {path}")
                m = self._member(dev)
                if not m:
                    return self._json({"error": "unknown wearer"}, HTTPStatus.NOT_FOUND)
                hist = store.history(dev)
                series = {}
                for sig, at, val, valid in hist:
                    series.setdefault(sig, []).append([at, val])
                evs = [e for e in store.events(device_id=dev, kinds=["alert", "alert_clear", "ack", "no_signal", "contact", "consent"], limit=100)]
                tile = views.build_tile(hub, m, t, staff=user["role"] == "staff")
                if user["role"] == "athlete":
                    tile["skin_c"] = (hub.state(dev).reading or {}).get("skin_c")   # own data: not hidden
                return self._json({"tile": tile, "series": series, "events": evs, "t": t,
                                   "consented": hub.is_consented(dev),
                                   "alerts_only": dev in hub.alerts_only,
                                   "kind": m["kind"]})
            if path == "/api/session":
                acc.require_staff(user, t, "GET /api/session")
                return self._json({"session_id": hub.session_id, "started": eng.t_to_iso(0), "t": t,
                                   "wearers": [{**m, "consented": hub.is_consented(m["device_id"]),
                                                "alerts_only": m["device_id"] in hub.alerts_only} for m in STATE["roster"]],
                                   "settings": {"trend_limit_c_per_5min": 1.0, "trend_window_s": 60,
                                                "absolute_limit_c": 38.5, "recovery_period_s": 60,
                                                "low_battery_pct": 20, "no_signal_after_s": views.NO_SIGNAL_AFTER_S},
                                   "events": store.events(limit=60), "denied": acc.denied_log[-20:][::-1],
                                   "demo_log": eng.log[-30:][::-1], "autoplay": eng.autoplay})
            if path == "/api/export.csv":
                acc.require_staff(user, t, "GET /api/export.csv")
                hub.flush()
                csv_text = store.export_csv(eng.t_to_iso(0), eng.t_to_iso).encode()
                store.add_event(t, "export", None, f"{len(csv_text)} bytes", user["username"])
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", f'attachment; filename="body-heat-{hub.session_id}.csv"')
                self.send_header("Content-Length", str(len(csv_text)))
                self.end_headers()
                return self.wfile.write(csv_text)
            if path == "/api/strings":
                return self._json({"strings": views.all_user_facing_strings(), "forbidden": views.FORBIDDEN_WORDS})
            if path == "/api/events":
                return self._sse(user)
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except AccessDenied as e:
            return self._json({"error": "access denied", "detail": str(e)}, HTTPStatus.FORBIDDEN)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        user, token = _user_from(self)
        hub, eng, acc, store = STATE["hub"], STATE["engine"], STATE["accounts"], STATE["store"]
        t = eng.t
        try:
            body = self._body()
            if path == "/api/login":
                tok = acc.sign_in(body.get("username", ""), body.get("password", ""))
                if not tok:
                    acc.denied_log.append((t, body.get("username", ""), "sign-in failed"))
                    return self._json({"error": "sign-in failed"}, HTTPStatus.UNAUTHORIZED)
                return self._json({"user": acc.user_for(tok)}, headers=[("Set-Cookie", f"bh_session={tok}; Path=/; HttpOnly; SameSite=Strict")])
            if path == "/api/logout":
                acc.sign_out(token)
                return self._json({"ok": True}, headers=[("Set-Cookie", "bh_session=; Path=/; Max-Age=0")])
            if path == "/api/ack":
                acc.require_staff(user, t, "POST /api/ack")
                hub.acknowledge(body["device_id"], user["username"], t)
                return self._json({"ok": True, "t": t, "user": user["username"]})
            if path == "/api/consent":
                dev = body["device_id"]
                if user and user["role"] == "athlete":
                    acc.require_device_access(user, dev, t, "POST /api/consent")
                    if body.get("consented"):
                        raise AccessDenied("athletes may withdraw; staff record consent")
                else:
                    acc.require_staff(user, t, "POST /api/consent")
                m = self._member(dev)
                hub.set_consent(dev, bool(body.get("consented")), t, user["username"])
                store.set_consent(m["participant_id"], bool(body.get("consented")), eng.t_to_iso(t))
                return self._json({"ok": True})
            if path == "/api/alerts_only":
                acc.require_staff(user, t, "POST /api/alerts_only")
                hub.set_alerts_only(body["device_id"], bool(body.get("on")))
                return self._json({"ok": True})
            if path.startswith("/api/demo/"):
                acc.require_staff(user, t, f"POST {path}")
                action = path.rsplit("/", 1)[-1]
                return self._json(eng.control(action, body.get("jersey"), user["username"]))
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except AccessDenied as e:
            return self._json({"error": "access denied", "detail": str(e)}, HTTPStatus.FORBIDDEN)
        except (KeyError, ValueError) as e:
            return self._json({"error": f"bad request: {e}"}, HTTPStatus.BAD_REQUEST)

    # -- Server-Sent Events: push only what changed ----------------------------------
    def _sse(self, user):
        if not user:
            return self._json({"error": "sign in first"}, HTTPStatus.UNAUTHORIZED)
        hub, eng, store, roster = STATE["hub"], STATE["engine"], STATE["store"], STATE["roster"]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        own = [m for m in roster if m["device_id"] == user["device_id"]] if user["role"] == "athlete" else roster
        last_tiles, last_strip, last_notices, last_status = None, None, None, None
        last_t = -1
        try:
            while True:
                t = eng.t
                if t != last_t:
                    last_t = t
                    tiles = views.build_roster(hub, own, t, staff=user["role"] == "staff")
                    if user["role"] == "athlete":
                        for tile in tiles:   # own data is never hidden from its owner
                            tile["skin_c"] = (hub.state(tile["device_id"]).reading or {}).get("skin_c")
                    strip = views.build_alert_strip(hub, own, t) if user["role"] == "staff" else []
                    notices = views.build_notices(hub, own, t) if user["role"] == "staff" else []
                    status = views.build_status(hub, t, store)
                    msg = {"t": t}
                    if last_tiles is None:
                        msg["tiles"] = tiles
                        msg["full"] = True
                    else:
                        changed = views.diff_tiles(last_tiles, tiles)
                        if changed:
                            msg["tiles"] = changed
                    if strip != last_strip:
                        msg["strip"] = strip
                    if notices != last_notices:
                        msg["notices"] = notices
                    msg["status"] = status
                    last_tiles, last_strip, last_notices, last_status = tiles, strip, notices, status
                    self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                    self.wfile.flush()
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8463)
    ap.add_argument("--no-autoplay", action="store_true", help="do not run the scripted scenario; presenter triggers everything")
    ap.add_argument("--analyzer", choices=["relay", "deterministic", "llm-explain", "llm-rolling"], default="deterministic")
    ap.add_argument("--real-provider", action="store_true", help="Explicit real provider opt-in; requires OPENROUTER_API_KEY")
    args = ap.parse_args(argv)
    if args.real_provider and not args.analyzer.startswith("llm-"):
        ap.error("--real-provider requires an llm analyzer")
    store, hub, accounts, roster, engine = build_demo(autoplay=not args.no_autoplay, analyzer=args.analyzer, real_provider=args.real_provider)
    STATE.update(store=store, hub=hub, accounts=accounts, roster=roster, engine=engine)
    engine.start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    print(f"Body Heat demo hub: http://localhost:{args.port}  (Ctrl-C to stop)")
    print("Sign in as k.smith / demo (staff) or athlete11 / demo (athlete). All data is synthetic.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        engine.stop()


if __name__ == "__main__":
    main()
