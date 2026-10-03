import calendar
import json
import os
import re
import sqlite3
import urllib.request
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).parent
DB_PATH = os.environ.get("TODO_DB", str(BASE / "todo.db"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "gemma2")
PORT = int(os.environ.get("PORT", "8000"))

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_RE = "|".join(DAYS)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                repeat TEXT NOT NULL DEFAULT '{"freq":"none"}',
                due TEXT
            );
            CREATE TABLE IF NOT EXISTS subtasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0
            );
            """
        )


def normalize_repeat(r):
    """Clean any repeat dict (from the model, the fallback or the UI)."""
    if not isinstance(r, dict):
        return {"freq": "none"}
    freq = str(r.get("freq", "none")).lower()
    if freq not in ("none", "daily", "weekly", "monthly"):
        return {"freq": "none"}
    if freq == "weekly":
        days = []
        for d in r.get("days") or []:
            if isinstance(d, int) and 0 <= d <= 6:
                days.append(d)
            elif isinstance(d, str) and d.strip().lower() in DAYS:
                days.append(DAYS.index(d.strip().lower()))
        days = sorted(set(days)) or [date.today().weekday()]
        return {"freq": "weekly", "days": days}
    if freq == "monthly":
        try:
            day = max(1, min(31, int(r.get("day", 1))))
        except (TypeError, ValueError):
            day = 1
        return {"freq": "monthly", "day": day}
    return {"freq": freq}


def next_occurrence(rule, after):
    """First date strictly after `after` that matches the repeat rule."""
    freq = rule["freq"]
    if freq == "daily":
        return after + timedelta(days=1)
    if freq == "weekly":
        for i in range(1, 8):
            d = after + timedelta(days=i)
            if d.weekday() in rule["days"]:
                return d
    if freq == "monthly":
        y, m = after.year, after.month
        for _ in range(2):
            day = min(rule["day"], calendar.monthrange(y, m)[1])
            cand = date(y, m, day)
            if cand > after:
                return cand
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        day = min(rule["day"], calendar.monthrange(y, m)[1])
        return date(y, m, day)
    return None


def repeat_label(rule):
    f = rule["freq"]
    if f == "daily":
        return "Every day"
    if f == "weekly":
        names = [DAYS[d][:3].title() for d in rule["days"]]
        return "Every " + ", ".join(names)
    if f == "monthly":
        return f"Monthly on day {rule['day']}"
    return ""


def refresh_repeats(conn):
    """Reset repeating tasks whose cycle has passed: uncheck everything and
    move the due date to the next occurrence."""
    today = date.today()
    for t in conn.execute("SELECT * FROM tasks WHERE repeat != '{\"freq\":\"none\"}'").fetchall():
        rule = normalize_repeat(json.loads(t["repeat"]))
        if rule["freq"] == "none":
            continue
        due = date.fromisoformat(t["due"]) if t["due"] else None
        if due is None or due < today:
            new_due = next_occurrence(rule, today - timedelta(days=1))
            conn.execute("UPDATE subtasks SET done = 0 WHERE task_id = ?", (t["id"],))
            conn.execute(
                "UPDATE tasks SET done = 0, due = ? WHERE id = ?",
                (new_due.isoformat(), t["id"]),
            )
    conn.commit()


def split_items(text):
    items = re.split(r",|;|\n|\band\b|&", text)
    return [i.strip(" .-") for i in items if i.strip(" .-")]


def fallback_parse(text):
    """Rule-based parser used when the local model isn't available."""
    low = text.lower()
    repeat = {"freq": "none"}
    strip_patterns = []

    m_days = re.search(
        rf"\b(?:every|each)\s+((?:{DAY_RE})s?(?:\s*(?:,|and|&)\s*(?:{DAY_RE})s?)*)", low
    )
    if re.search(r"\bweekdays?\b", low):
        repeat = {"freq": "weekly", "days": [0, 1, 2, 3, 4]}
        strip_patterns.append(r"\b(?:every\s+)?weekdays?\b")
    elif re.search(r"\bweekends?\b", low):
        repeat = {"freq": "weekly", "days": [5, 6]}
        strip_patterns.append(r"\b(?:every\s+)?weekends?\b")
    elif m_days:
        days = [i for i, d in enumerate(DAYS) if d in m_days.group(1)]
        repeat = {"freq": "weekly", "days": days}
        strip_patterns.append(
            rf"\b(?:every|each)\s+(?:{DAY_RE})s?(?:\s*(?:,|and|&)\s*(?:{DAY_RE})s?)*"
        )
    elif re.search(r"\b(?:every|each)\s+day\b|\bdaily\b", low):
        repeat = {"freq": "daily"}
        strip_patterns.append(r"\b(?:every|each)\s+day\b|\bdaily\b")
    elif re.search(r"\b(?:every|each)\s+month\b|\bmonthly\b", low):
        m = re.search(r"(\d{1,2})(?:st|nd|rd|th)", low)
        repeat = {"freq": "monthly", "day": int(m.group(1)) if m else 1}
        strip_patterns.append(r"\b(?:every|each)\s+month\b|\bmonthly\b")
        strip_patterns.append(r"\b(?:on\s+)?(?:the\s+)?\d{1,2}(?:st|nd|rd|th)\b(?:\s+of\b)?")
    elif re.search(r"\b(?:every|each)\s+week\b|\bweekly\b", low):
        repeat = {"freq": "weekly", "days": [date.today().weekday()]}
        strip_patterns.append(r"\b(?:every|each)\s+week\b|\bweekly\b")

    cleaned = text
    for p in strip_patterns:
        cleaned = re.sub(p, "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;")

    head, tail = cleaned, ""
    m = re.search(r":|\s-\s|\bincluding\b|\bwith\b|\bwhich has\b", cleaned, flags=re.I)
    if m:
        head, tail = cleaned[: m.start()], cleaned[m.end():]
    title = head.strip(" ,.:-").capitalize() or text.strip()
    return {
        "title": title,
        "subtasks": [s.capitalize() for s in split_items(tail)],
        "repeat": normalize_repeat(repeat),
    }


def ollama_json(prompt):
    body = json.dumps(
        {"model": MODEL, "prompt": prompt, "stream": False, "format": "json"}
    ).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate", body, {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(json.loads(resp.read())["response"])


PARSE_PROMPT = """Turn the user's to-do sentence into JSON with exactly these keys:
"title": short task title (string),
"subtasks": list of short subtask strings (empty list if none),
"repeat": {{"freq": "none" | "daily" | "weekly" | "monthly",
           "days": [weekday names, only for weekly],
           "day": day of month number, only for monthly}}.
Only use information in the sentence. Do not invent subtasks.

Sentence: {text}"""


def parse_text(text):
    try:
        data = ollama_json(PARSE_PROMPT.format(text=text))
        title = str(data["title"]).strip()
        subs = [str(s).strip() for s in data.get("subtasks", []) if str(s).strip()]
        if not title:
            raise ValueError("empty title")
        return {
            "title": title,
            "subtasks": subs,
            "repeat": normalize_repeat(data.get("repeat")),
            "source": f"{MODEL} (local)",
        }
    except Exception:
        out = fallback_parse(text)
        out["source"] = "offline rules"
        return out


def suggest_subtasks(title):
    data = ollama_json(
        'Give 3 to 6 short, concrete subtasks for this task. '
        'Reply as JSON: {"subtasks": ["..."]}. Task: ' + title
    )
    return [str(s).strip() for s in data.get("subtasks", []) if str(s).strip()][:8]


def ollama_status():
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=1.5) as r:
            names = [m["name"] for m in json.loads(r.read()).get("models", [])]
        installed = any(n.split(":")[0] == MODEL.split(":")[0] for n in names)
        return {"ollama": True, "model": MODEL, "model_installed": installed}
    except Exception:
        return {"ollama": False, "model": MODEL, "model_installed": False}


def serialize(conn):
    refresh_repeats(conn)
    tasks = []
    for t in conn.execute("SELECT * FROM tasks ORDER BY done, id").fetchall():
        rule = normalize_repeat(json.loads(t["repeat"]))
        subs = [
            {"id": s["id"], "title": s["title"], "done": bool(s["done"])}
            for s in conn.execute(
                "SELECT * FROM subtasks WHERE task_id = ? ORDER BY id", (t["id"],)
            )
        ]
        tasks.append(
            {
                "id": t["id"],
                "title": t["title"],
                "done": bool(t["done"]),
                "due": t["due"],
                "repeat": rule,
                "repeat_label": repeat_label(rule),
                "subtasks": subs,
            }
        )
    return tasks


def create_task(conn, title, subtasks, repeat):
    rule = normalize_repeat(repeat)
    due = None
    if rule["freq"] != "none":
        due = next_occurrence(rule, date.today() - timedelta(days=1)).isoformat()
    cur = conn.execute(
        "INSERT INTO tasks (title, repeat, due) VALUES (?, ?, ?)",
        (title.strip(), json.dumps(rule), due),
    )
    for s in subtasks:
        if s.strip():
            conn.execute(
                "INSERT INTO subtasks (task_id, title) VALUES (?, ?)", (cur.lastrowid, s.strip())
            )
    conn.commit()
    return cur.lastrowid


def toggle_subtask(conn, sid):
    row = conn.execute("SELECT * FROM subtasks WHERE id = ?", (sid,)).fetchone()
    if not row:
        return
    conn.execute("UPDATE subtasks SET done = 1 - done WHERE id = ?", (sid,))
    left = conn.execute(
        "SELECT COUNT(*) FROM subtasks WHERE task_id = ? AND done = 0", (row["task_id"],)
    ).fetchone()[0]
    conn.execute("UPDATE tasks SET done = ? WHERE id = ?", (1 if left == 0 else 0, row["task_id"]))
    conn.commit()


def toggle_task(conn, tid):
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    if not row:
        return
    new = 1 - row["done"]
    conn.execute("UPDATE tasks SET done = ? WHERE id = ?", (new, tid))
    conn.execute("UPDATE subtasks SET done = ? WHERE task_id = ?", (new, tid))
    conn.commit()


# ------------------------------------------------------------------ http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (BASE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/tasks":
            with db() as c:
                self.send_json(serialize(c))
        elif self.path == "/api/status":
            self.send_json(ollama_status())
        elif self.path == "/api/export":
            with db() as c:
                data = json.dumps(serialize(c), indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition", 'attachment; filename="tododo-export.json"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            data = self.read_json()
            if self.path == "/api/parse":
                text = str(data.get("text", "")).strip()
                if not text:
                    return self.send_json({"error": "empty text"}, 400)
                return self.send_json(parse_text(text))
            if self.path == "/api/suggest":
                try:
                    return self.send_json({"subtasks": suggest_subtasks(str(data.get("title", "")))})
                except Exception:
                    return self.send_json({"error": "The local model isn't running."}, 503)
            if self.path == "/api/tasks":
                title = str(data.get("title", "")).strip()
                if not title:
                    return self.send_json({"error": "title required"}, 400)
                with db() as c:
                    tid = create_task(c, title, data.get("subtasks", []), data.get("repeat"))
                return self.send_json({"id": tid}, 201)
            m = re.fullmatch(r"/api/subtasks/(\d+)/toggle", self.path)
            if m:
                with db() as c:
                    toggle_subtask(c, int(m.group(1)))
                return self.send_json({"ok": True})
            m = re.fullmatch(r"/api/tasks/(\d+)/toggle", self.path)
            if m:
                with db() as c:
                    toggle_task(c, int(m.group(1)))
                return self.send_json({"ok": True})
            self.send_json({"error": "not found"}, 404)
        except Exception as e:  # keep the server alive on bad input
            self.send_json({"error": str(e)}, 500)

    def do_DELETE(self):
        m = re.fullmatch(r"/api/tasks/(\d+)", self.path)
        if not m:
            return self.send_json({"error": "not found"}, 404)
        with db() as c:
            c.execute("DELETE FROM tasks WHERE id = ?", (int(m.group(1)),))
        self.send_json({"ok": True})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # The browser closed the connection early (tab refreshed or closed).
        # Harmless, so don't print a traceback.
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


if __name__ == "__main__":
    init_db()
    print(f"Tododo running at http://localhost:{PORT}  (model: {MODEL})")
    print("Press Ctrl+C to stop.")
    try:
        Server(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nTododo stopped. Bye!")