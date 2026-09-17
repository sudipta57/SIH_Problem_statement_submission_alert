"""GET /api/check  -> runs one check.
Auth: header 'Authorization: Bearer <CRON_SECRET>'  or  query ?key=<CRON_SECRET>
Extras: ?dry=1 (no mail, no state write), ?test=1 (send a test mail)"""
import hmac, json, os, sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(__file__))
import _core  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        secret = os.environ.get("CRON_SECRET", "")
        q = parse_qs(urlparse(self.path).query)
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip() \
            or (q.get("key") or [""])[0]
        if not secret or not hmac.compare_digest(given, secret):
            return self._send(401, {"ok": False, "error": "unauthorized"})

        try:
            if q.get("test") == ["1"]:
                _core.send_mail("[SIH watch] test mail", "Vercel watcher can send mail.")
                return self._send(200, {"ok": True, "sent": "test mail"})
            result = _core.run(dry_run=q.get("dry") == ["1"])
            # 200 even on fetch failure: failures are tracked in Redis; a 5xx would just
            # make the scheduler hammer the site with retries.
            return self._send(200, result)
        except Exception as e:  # noqa: BLE001  (config errors, Redis down, SMTP auth)
            return self._send(500, {"ok": False, "error": str(e)})
