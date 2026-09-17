"""Core logic for the Vercel version. Files starting with _ are not exposed as routes."""
import json, os, re, smtplib, ssl, time, urllib.request
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

IST = timezone(timedelta(hours=5, minutes=30))
URL = "https://sih.gov.in/sih2026PS"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://sih.gov.in/",
}


def env(name, default=None):
    return os.environ.get(name, default)


# ------------------------------------------------------------ fetch / parse
def fetch_html() -> str:
    test_file = env("SIH_TEST_HTML")          # local testing only
    if test_file:
        return open(test_file, encoding="utf-8", errors="replace").read()
    last = None
    for attempt in range(2):                  # keep total time well under maxDuration
        try:
            req = urllib.request.Request(URL, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as r:
                text = r.read().decode("utf-8", errors="replace")
            if "dataTablePS" not in text:
                raise RuntimeError("page has no #dataTablePS (blocked or layout changed)")
            return text
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt == 0:
                time.sleep(5)
    raise RuntimeError(f"fetch failed: {last}")


TAG_RE = re.compile(r"<[^>]+>")
COUNT_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def parse_count(html: str, ps_id: str):
    ps_cell = re.compile(r"<td[^>]*>\s*" + re.escape(ps_id) + r"\s*</td>", re.I)
    next_td = re.compile(r"<td[^>]*>(.*?)</td>", re.I | re.S)
    for m in ps_cell.finditer(html):
        n = next_td.search(html, m.end())
        if n:
            c = COUNT_RE.search(TAG_RE.sub("", n.group(1)))
            if c:
                return int(c.group(1)), int(c.group(2))
    raise ValueError(f"{ps_id} not found in page (or count cell format changed)")


def milestone_for(count, start, step, cap):
    if count >= cap:
        return cap
    if count < start:
        return 0
    return start + ((count - start) // step) * step


# ------------------------------------------------------------ state (Upstash Redis REST)
class Store:
    def __init__(self):
        self.url = (env("UPSTASH_REDIS_REST_URL") or env("KV_REST_API_URL") or "").rstrip("/")
        self.token = env("UPSTASH_REDIS_REST_TOKEN") or env("KV_REST_API_TOKEN")
        self._mem = {}                        # fallback for local tests only
        if not (self.url and self.token) and not env("SIH_TEST_HTML"):
            raise RuntimeError("Redis not configured (UPSTASH_REDIS_REST_URL / _TOKEN)")

    def _call(self, path, body=None):
        if not (self.url and self.token):
            return None
        req = urllib.request.Request(f"{self.url}/{path}", data=body,
                                     headers={"Authorization": f"Bearer {self.token}"},
                                     method="POST" if body is not None else "GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())["result"]

    def get(self, key):
        if not self.url:
            return self._mem.get(key)
        raw = self._call(f"get/{key}")
        return json.loads(raw) if raw else None

    def set(self, key, value):
        if not self.url:
            self._mem[key] = value
            return
        self._call(f"set/{key}", json.dumps(value).encode())

    def lock(self, key, seconds=90):
        """True if we got the lock (stops two overlapping triggers double-mailing)."""
        if not self.url:
            return True
        return self._call(f"set/{key}/1/nx/ex/{seconds}") == "OK"

    def unlock(self, key):
        if self.url:
            self._call(f"del/{key}")


# ------------------------------------------------------------ mail
def send_mail(subject, body):
    host, port = env("SMTP_HOST", "smtp.gmail.com"), int(env("SMTP_PORT", "465"))
    user, pwd = env("SMTP_USER"), env("SMTP_PASS")
    to = [a.strip() for a in (env("MAIL_TO") or "").split(",") if a.strip()]
    if not (user and pwd and to):
        raise RuntimeError("SMTP_USER / SMTP_PASS / MAIL_TO not set")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(to)
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
            s.login(user, pwd); s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls(context=ctx); s.login(user, pwd); s.send_message(msg)


# ------------------------------------------------------------ one check
def run(dry_run=False, store=None, mailer=send_mail):
    ps_id = env("PS_ID", "SIH26166").upper()
    start, step = int(env("START_AT", "50")), int(env("STEP", "25"))
    fail_alert_after = int(env("FAIL_ALERT_AFTER", "6"))
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    store = store or Store()
    key = f"sihwatch:{ps_id}"

    if not dry_run and not store.lock(f"{key}:lock"):
        return {"ok": True, "skipped": "another run in progress"}
    try:
        st = store.get(key) or {"last_notified": 0, "fail_streak": 0}
        out = {"ps": ps_id, "checked": now, "dry_run": dry_run}

        try:
            count, cap = parse_count(fetch_html(), ps_id)
        except Exception as e:  # noqa: BLE001
            st["fail_streak"] = st.get("fail_streak", 0) + 1
            out.update(ok=False, error=str(e), fail_streak=st["fail_streak"])
            if not dry_run:
                if st["fail_streak"] == fail_alert_after:
                    mailer(f"[SIH watch] {ps_id}: watcher failing",
                           f"{st['fail_streak']} consecutive failures.\nLast error: {e}\n"
                           f"Check {URL} manually.\n{now}")
                store.set(key, st)
            return out

        if st.get("fail_streak", 0) >= fail_alert_after and not dry_run:
            mailer(f"[SIH watch] {ps_id}: watcher recovered", f"Current count: {count}/{cap}\n{now}")
        st["fail_streak"] = 0

        prev, last = st.get("last_count"), st.get("last_notified", 0)
        hit = milestone_for(count, start, step, cap)
        out.update(ok=True, count=count, cap=cap, prev=prev, last_notified=last, milestone=hit)

        if hit > last:
            skipped = [m for m in range(max(start, last + step), hit, step)]
            subject = (f"[SIH watch] {ps_id} is FULL: {count}/{cap}" if hit >= cap
                       else f"[SIH watch] {ps_id} crossed {hit}: now {count}/{cap}")
            body = (f"{ps_id} now has {count} of {cap} idea submissions.\nMilestone crossed: {hit}\n"
                    + (f"(Also passed since last alert: {', '.join(map(str, skipped))})\n" if skipped else "")
                    + f"Previous check: {prev}\n"
                      f"Next alert at: {'none (cap reached)' if hit >= cap else min(hit + step, cap)}\n\n"
                      f"Source: {URL}\nChecked: {now}\n")
            if dry_run:
                out["would_send"] = subject
            else:
                mailer(subject, body)
                st["last_notified"] = hit
                out["sent"] = subject
        else:
            out["next_alert_at"] = start if last < start else min(last + step, cap)

        if not dry_run:
            st.update(last_count=count, last_checked=now)
            store.set(key, st)
        return out
    finally:
        if not dry_run:
            store.unlock(f"{key}:lock")
