import html
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import psycopg
from dotenv import load_dotenv

load_dotenv()

DB_URL = os.getenv("DATABASE_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))
HOST = os.getenv("HOST", "0.0.0.0")
INSERT_INTERVAL_SEC = float(os.getenv("INSERT_INTERVAL_SEC", "5"))
MAX_ROWS = int(os.getenv("MAX_ROWS", "20"))
EGRESS_TEST_URL = os.getenv("EGRESS_TEST_URL", "http://example.com").strip()
EGRESS_TEST_INTERVAL_SEC = float(os.getenv("EGRESS_TEST_INTERVAL_SEC", "10"))
EGRESS_TEST_TIMEOUT_SEC = float(os.getenv("EGRESS_TEST_TIMEOUT_SEC", "3"))

PATIENT_NAMES = [
    "Sleve McDichael",
    "Willie Dustice",
    "Onson Sweemey",
    "Jeromy Gride",
    "Darryl Archideld",
    "Scott Dorque",
    "Anatoli Smorin",
    "Shown Furcotte",
    "Rey McSriff",
    "Dean Wesrey",
    "Glenallen Mixon",
    "Mike Truk",
    "Mario McRlwain",
    "Dwigt Rortugal",
    "Raul Chamgerlain",
    "Tim Sandaele",
    "Kevin Nogilny",
    "Karl Dandleton",
    "Tony Smehrik",
    "Mike Sernandez",
    "Bobson Dugnutt",
    "Todd Bonzalez",
]

UPCOMING_PROCEDURES = [
    "MRI Scan (Spleen)",
    "CT Scan w/ Red 40 Contrast Dye",
    "Posterior Ultrasound",
    "COVID-19 Vaccine (Incl. Govt. Tracking Nanochip)",
    "Blood Panel Debate",
    "Kidney Stress Test",
    "Colonoscopy (Extra Thorough)",
    "Psychosomatic Therapy Intake",
    "Rib Cage Transplant",
    "Post-Black-Op Checkup",
]


def _db_enabled() -> bool:
    return bool(DB_URL)


def _randomized_reminder_message() -> str:
    procedure = random.choice(UPCOMING_PROCEDURES)
    patient_name = random.choice(PATIENT_NAMES)
    return f"Reminder – Upcoming {procedure} for {patient_name}"


_REMINDER_PREFIX = "Reminder – Upcoming "
_REMINDER_SEP = " for "


def _render_reminder_html(text: str) -> str:
    if not text.startswith(_REMINDER_PREFIX):
        return html.escape(text)
    rest = text[len(_REMINDER_PREFIX) :]
    if _REMINDER_SEP not in rest:
        return html.escape(text)
    procedure, patient = rest.split(_REMINDER_SEP, 1)
    return (
        html.escape(_REMINDER_PREFIX)
        + "<strong>"
        + html.escape(procedure)
        + "</strong>"
        + html.escape(_REMINDER_SEP)
        + "<strong>"
        + html.escape(patient)
        + "</strong>"
    )


class ReminderStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._schema_ready = False
        self._connected = False
        self._last_error: Optional[str] = None

    def last_error(self) -> Optional[str]:
        with self._lock:
            return self._last_error

    def schema_ready(self) -> bool:
        with self._lock:
            return self._schema_ready

    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def _set_error(self, msg: Optional[str]) -> None:
        with self._lock:
            self._last_error = msg

    def _set_schema_ready(self, ready: bool) -> None:
        with self._lock:
            self._schema_ready = ready

    def _set_connected(self, connected: bool) -> None:
        with self._lock:
            self._connected = connected

    def init(self) -> None:
        if not _db_enabled():
            return
        try:
            with psycopg.connect(DB_URL, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        create table if not exists reminders (
                            id bigserial primary key,
                            created_at timestamptz not null default now(),
                            text text not null
                        );
                    """
                    )
            self._set_schema_ready(True)
            self._set_connected(True)
            self._set_error(None)
            print("DB init ok: reminders table ready")
        except Exception as e:
            self._set_schema_ready(False)
            self._set_connected(False)
            err = f"DB init failed: {e!r}"
            self._set_error(err)
            print(err)

    def insert_reminder(self, text: str) -> None:
        if not _db_enabled():
            return
        if not self.schema_ready():
            self.init()
            if not self.schema_ready():
                return
        try:
            with psycopg.connect(DB_URL, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("insert into reminders (text) values (%s);", (text,))
            self._set_connected(True)
            self._set_error(None)
            print(f"DB insert ok: {text}")
        except Exception as e:
            self._set_connected(False)
            err = f"DB insert failed: {e!r}"
            self._set_error(err)
            print(err)

    def fetch_latest(self, limit: int) -> list[tuple[str, str]]:
        if not _db_enabled():
            return []
        if not self.schema_ready():
            self.init()
            if not self.schema_ready():
                return []
        try:
            with psycopg.connect(DB_URL, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "select created_at::text, text from reminders order by id desc limit %s;",
                        (limit,),
                    )
                    rows = cur.fetchall()
            self._set_connected(True)
            self._set_error(None)
            return [(str(r[0]), str(r[1])) for r in rows]
        except Exception as e:
            self._set_connected(False)
            self._set_error(f"DB fetch failed: {e!r}")
            return []


class EgressProbe:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ok: Optional[bool] = None
        self._last_error: Optional[str] = None
        self._last_checked_at: Optional[str] = None

    def snapshot(self) -> tuple[Optional[bool], Optional[str], Optional[str]]:
        with self._lock:
            return self._ok, self._last_error, self._last_checked_at

    def check(self) -> None:
        req = Request(EGRESS_TEST_URL, method="HEAD")
        now = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        try:
            with urlopen(req, timeout=EGRESS_TEST_TIMEOUT_SEC) as resp:
                status_code = getattr(resp, "status", None) or resp.getcode()
            ok = 200 <= int(status_code) < 400
            err = None if ok else f"HTTP status {status_code}"
        except Exception as e:
            ok = False
            err = repr(e)

        with self._lock:
            self._ok = ok
            self._last_error = err
            self._last_checked_at = now


store = ReminderStore()
egress = EgressProbe()


def reminder_loop() -> None:
    # Insert immediately, then continue on interval
    while True:
        if _db_enabled():
            msg = _randomized_reminder_message()
            store.insert_reminder(msg)
        time.sleep(INSERT_INTERVAL_SEC)


def egress_loop() -> None:
    # Probe immediately, then continue on interval
    while True:
        egress.check()
        time.sleep(EGRESS_TEST_INTERVAL_SEC)


PAGE_STYLE = """
:root { color-scheme: light; }
body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 0; }
header { padding: 24px; border-bottom: 1px solid #e5e7eb; background: #fafafa; }
main { max-width: 860px; margin: 0 auto; padding: 24px; }
.card { border: 1px solid #e5e7eb; border-radius: 14px; padding: 18px; box-shadow: 0 1px 2px rgba(0,0,0,.04); }
h1 { margin: 0 0 6px 0; font-size: 22px; }
p.sub { margin: 0; color: #6b7280; font-size: 14px; }
.badge { display:inline-block; padding: 4px 10px; border-radius: 999px; font-size: 12px; border:1px solid #e5e7eb; background:#fff; }
.badge.ok { border-color:#bbf7d0; background:#f0fdf4; color:#166534; }
.badge.bad { border-color:#fecaca; background:#fef2f2; color:#991b1b; }
.badge.warn { border-color:#fde68a; background:#fffbeb; color:#92400e; }
.list { margin: 14px 0 6px; padding: 0; list-style: none; }
.list li { padding: 10px 12px; border: 1px solid #f3f4f6; border-radius: 10px; margin-bottom: 10px; background: #fff; }
.list li:last-child { margin-bottom: 0; }
.meta { color:#6b7280; font-size: 12px; }
code { background: #f3f4f6; padding: 2px 6px; border-radius: 8px; }
"""


def render_page(
    rows: list[tuple[str, str]],
    db_connected: bool,
    db_error: Optional[str],
    egress_ok: Optional[bool],
    egress_error: Optional[str],
    egress_checked_at: Optional[str],
) -> bytes:
    safe_err = html.escape(db_error) if db_error else ""
    if db_connected:
        db_badge = '<span class="badge ok">DB connected</span>'
    else:
        db_badge = '<span class="badge bad">DB disconnected</span>'

    safe_egress_error = html.escape(egress_error) if egress_error else ""
    safe_egress_checked_at = (
        html.escape(egress_checked_at) if egress_checked_at else "never"
    )
    if egress_ok is True:
        egress_badge = '<span class="badge ok">Internet egress OK</span>'
    elif egress_ok is False:
        egress_badge = '<span class="badge bad">Internet egress failed</span>'
    else:
        egress_badge = '<span class="badge warn">Internet egress unknown</span>'

    items = ""
    if not db_connected:
        items = "<li><div style='margin-bottom:4px;'><strong>Database disconnected</strong></div><div class='meta'>Make sure to set <code>DATABASE_URL</code> to enable reminders.</div></li>"
        if safe_err:
            items += f"<li><div style='margin-bottom:4px;'><strong>Last DB error:</strong></div><div class='meta'>{safe_err}</div></li>"
    else:
        if safe_err:
            items += f"<li><div><strong>DB warning</strong></div><div class='meta'>{safe_err}</div></li>"
        for created_at, text in rows:
            items += (
                "<li>"
                f"<div>{_render_reminder_html(text)}</div>"
                f"<div class='meta'>{html.escape(created_at)}</div>"
                "</li>"
            )
        if not rows:
            items += "<li><div><strong>No reminders yet.</strong></div><div class='meta'>Wait for the next insert interval and refresh.</div></li>"

    html_doc = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>PatientPing</title>
  <style>{PAGE_STYLE}</style>
</head>
<body>
  <header>
    <main style="padding:0;max-width:860px;margin:0 auto;">
      <h1>PatientPing</h1>
      <p class="sub">Because memory is not a medical device. Ping now, panic never.</p>
    </main>
  </header>
  <main>
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;">
        <div>
          <div class="meta" style="margin-bottom:6px;">Status</div>
          {db_badge}
          {egress_badge}
        </div>
        <div class="meta">DB insert interval: <code>{INSERT_INTERVAL_SEC}</code>s • Showing latest <code>{MAX_ROWS}</code></div>
      </div>
      <div class="meta" style="margin-top:8px;">Egress target: <code>{html.escape(EGRESS_TEST_URL)}</code> • Last checked: {safe_egress_checked_at}</div>
      {f"<div class='meta' style='margin-top:4px;'>Last egress error: {safe_egress_error}</div>" if safe_egress_error else ""}
      <ul class="list">
        {items}
      </ul>
    </div>
  </main>
</body>
</html>
"""
    return html_doc.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path not in ("/", "/healthz"):
            payload = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if path == "/healthz":
            payload = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        rows = store.fetch_latest(MAX_ROWS) if _db_enabled() else []
        db_connected = _db_enabled() and store.connected()
        egress_ok, egress_error, egress_checked_at = egress.snapshot()
        payload = render_page(
            rows,
            db_connected,
            store.last_error(),
            egress_ok,
            egress_error,
            egress_checked_at,
        )

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        # quieter logs
        return


def main() -> None:
    if _db_enabled():
        store.init()

    egress.check()

    t = threading.Thread(target=reminder_loop, daemon=True)
    t.start()

    t_egress = threading.Thread(target=egress_loop, daemon=True)
    t_egress.start()

    httpd = HTTPServer((HOST, PORT), Handler)
    print(
        f"PatientPing listening on http://{HOST}:{PORT}  (db={'on' if _db_enabled() else 'off'})"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
