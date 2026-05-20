import json
import os
import time
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

CTFD_URL = os.getenv("CTFD_URL", "").rstrip("/")
CTFD_TOKEN = os.getenv("CTFD_TOKEN", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

BOT_HOST = os.getenv("BOT_HOST", "0.0.0.0")
BOT_PORT = int(os.getenv("BOT_PORT", "3892"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "20"))

ANNOUNCE_SOLVES = os.getenv("ANNOUNCE_SOLVES", "false").lower() == "true"
SKIP_EXISTING_ON_FIRST_RUN = os.getenv("SKIP_EXISTING_ON_FIRST_RUN", "true").lower() == "true"
ANNOUNCE_HIDDEN_CHALLENGES = os.getenv("ANNOUNCE_HIDDEN_CHALLENGES", "false").lower() == "true"

BOT_NAME = os.getenv("BOT_NAME", "CTFd Treasure Watcher")
BOT_AVATAR_URL = os.getenv("BOT_AVATAR_URL", "")
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

session = requests.Session()
session.headers.update({
    "Authorization": f"Token {CTFD_TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "ctfd-discord-treasure-bot/1.0",
})

runtime: Dict[str, Any] = {
    "started_at": None,
    "last_poll": None,
    "last_error": None,
    "last_ctfd_check": None,
    "last_discord_test": None,
    "new_challenge_sent": 0,
    "first_blood_sent": 0,
    "solve_sent": 0,
}

cache_challenges: Dict[int, Dict[str, Any]] = {}
cache_users: Dict[int, Dict[str, Any]] = {}
cache_teams: Dict[int, Dict[str, Any]] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def empty_state() -> Dict[str, Any]:
    return {
        "initialized": False,
        "seen_challenges": [],
        "seen_solves": [],
        "first_blood_challenges": [],
        "last_run": None,
    }


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return empty_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    state = empty_state()
    state.update(data if isinstance(data, dict) else {})
    return state


def save_state(state: Dict[str, Any]) -> None:
    state["last_run"] = now_iso()
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def api_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not CTFD_URL:
        raise RuntimeError("CTFD_URL kosong")
    if not CTFD_TOKEN:
        raise RuntimeError("CTFD_TOKEN kosong")

    url = f"{CTFD_URL}{path}"
    r = session.get(url, params=params, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"CTFd API {r.status_code}: {url} -> {r.text[:500]}")

    try:
        data = r.json()
    except Exception as exc:
        raise RuntimeError(f"CTFd API bukan JSON: {url} -> {r.text[:500]}") from exc

    if data.get("success") is False:
        raise RuntimeError(f"CTFd API success=false: {url} -> {data}")
    return data


def api_list(path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    params = dict(params or {})
    params.setdefault("per_page", 100)
    page = 1
    out: List[Dict[str, Any]] = []

    while True:
        params["page"] = page
        data = api_get(path, params=params)
        items = data.get("data", [])
        if isinstance(items, list):
            out.extend(items)

        pagination = data.get("meta", {}).get("pagination", {})
        next_page = pagination.get("next")
        if not next_page:
            break
        page = int(next_page)

    return out


def safe(value: Any, limit: int = 180) -> str:
    text = str(value if value is not None else "-")
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere").strip()
    return text[:limit - 3] + "..." if len(text) > limit else text


def challenge_visible(ch: Dict[str, Any]) -> bool:
    if ANNOUNCE_HIDDEN_CHALLENGES:
        return True
    state = str(ch.get("state", "visible")).lower()
    return state == "visible"


def discord_send(embed: Dict[str, Any]) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL kosong")

    payload: Dict[str, Any] = {
        "username": BOT_NAME,
        "allowed_mentions": {"parse": []},
        "embeds": [embed],
    }
    if BOT_AVATAR_URL:
        payload["avatar_url"] = BOT_AVATAR_URL

    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
    if r.status_code == 429:
        try:
            retry_after = float(r.json().get("retry_after", 2))
        except Exception:
            retry_after = 2
        time.sleep(retry_after)
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)

    if r.status_code not in (200, 204):
        raise RuntimeError(f"Discord webhook {r.status_code}: {r.text[:500]}")


def get_challenge(challenge_id: int) -> Dict[str, Any]:
    if challenge_id in cache_challenges:
        return cache_challenges[challenge_id]
    data = api_get(f"/api/v1/challenges/{challenge_id}")
    ch = data.get("data", {})
    if isinstance(ch, dict):
        cache_challenges[challenge_id] = ch
    return ch


def get_user(user_id: int) -> Dict[str, Any]:
    if user_id in cache_users:
        return cache_users[user_id]
    data = api_get(f"/api/v1/users/{user_id}")
    user = data.get("data", {})
    if isinstance(user, dict):
        cache_users[user_id] = user
    return user


def get_team(team_id: int) -> Dict[str, Any]:
    if team_id in cache_teams:
        return cache_teams[team_id]
    data = api_get(f"/api/v1/teams/{team_id}")
    team = data.get("data", {})
    if isinstance(team, dict):
        cache_teams[team_id] = team
    return team


def solve_id(solve: Dict[str, Any]) -> int:
    for key in ("id", "submission_id"):
        if solve.get(key) is not None:
            return int(solve[key])

    raw = f"{solve.get('challenge_id')}-{solve.get('account_id')}-{solve.get('user_id')}-{solve.get('team_id')}-{solve.get('date')}"
    return abs(hash(raw))


def challenge_id_from_solve(solve: Dict[str, Any]) -> Optional[int]:
    val = solve.get("challenge_id")
    if isinstance(val, int):
        return val
    obj = solve.get("challenge")
    if isinstance(obj, dict) and obj.get("id") is not None:
        return int(obj["id"])
    return None


def solver_name(solve: Dict[str, Any]) -> str:
    for key in ("account", "user", "team"):
        obj = solve.get(key)
        if isinstance(obj, dict) and obj.get("name"):
            return safe(obj["name"], 80)

    team_id = solve.get("team_id")
    user_id = solve.get("user_id")
    account_id = solve.get("account_id")

    try:
        if team_id:
            return safe(get_team(int(team_id)).get("name", f"Team #{team_id}"), 80)
        if user_id:
            return safe(get_user(int(user_id)).get("name", f"User #{user_id}"), 80)
        if account_id:
            return safe(get_user(int(account_id)).get("name", f"User #{account_id}"), 80)
    except Exception:
        pass

    return "Unknown Solver"


def fetch_challenges() -> List[Dict[str, Any]]:
    data = api_get("/api/v1/challenges")
    items = data.get("data", [])
    if not isinstance(items, list):
        return []
    for ch in items:
        if isinstance(ch, dict) and ch.get("id") is not None:
            cache_challenges[int(ch["id"])] = ch
    return [x for x in items if isinstance(x, dict)]


def fetch_solves() -> List[Dict[str, Any]]:
    try:
        items = api_list("/api/v1/solves")
        if isinstance(items, list):
            return items
    except Exception as exc:
        log(f"[WARN] /api/v1/solves gagal, fallback submissions: {exc}")

    return api_list("/api/v1/submissions", params={"field": "type", "q": "correct"})


def embed_test() -> Dict[str, Any]:
    return {
        "title": "🏴‍☠️ CTFd Treasure Watcher Online",
        "description": "Discord webhook berhasil. Bot siap memantau challenge dan first blood.",
        "color": 0xF7C948,
        "fields": [
            {"name": "🌐 CTFd", "value": CTFD_URL or "-", "inline": False},
            {"name": "🧭 HTTP Port", "value": str(BOT_PORT), "inline": True},
            {"name": "⏱️ Poll", "value": f"{POLL_INTERVAL}s", "inline": True},
        ],
        "footer": {"text": "ctfd-treasure-theme • watcher"},
        "timestamp": now_iso(),
    }


def embed_new_challenge(ch: Dict[str, Any]) -> Dict[str, Any]:
    cid = ch.get("id")
    return {
        "title": "🗺️ New Island Discovered!",
        "description": f"Challenge baru muncul di Treasure Map.\n\n**{safe(ch.get('name'), 120)}**",
        "color": 0xF1C40F,
        "fields": [
            {"name": "🏝️ Category", "value": safe(ch.get("category"), 80), "inline": True},
            {"name": "💰 Points", "value": safe(ch.get("value"), 20), "inline": True},
            {"name": "✅ Solves", "value": safe(ch.get("solves", 0), 20), "inline": True},
            {"name": "🧭 Route", "value": f"{CTFD_URL}/challenges#{cid}", "inline": False},
        ],
        "footer": {"text": "ctfd-treasure-theme • new challenge"},
        "timestamp": now_iso(),
    }


def embed_first_blood(solve: Dict[str, Any], ch: Dict[str, Any]) -> Dict[str, Any]:
    cid = ch.get("id") or challenge_id_from_solve(solve)
    solver = solver_name(solve)
    return {
        "title": "🩸 FIRST BLOOD!",
        "description": f"**{solver}** menjadi orang pertama yang claim treasure.",
        "color": 0xE74C3C,
        "fields": [
            {"name": "🏴‍☠️ Solver", "value": solver, "inline": True},
            {"name": "🏝️ Challenge", "value": safe(ch.get("name"), 120), "inline": True},
            {"name": "📦 Category", "value": safe(ch.get("category"), 80), "inline": True},
            {"name": "💰 Points", "value": safe(ch.get("value"), 20), "inline": True},
            {"name": "🧭 Route", "value": f"{CTFD_URL}/challenges#{cid}", "inline": False},
        ],
        "footer": {"text": "ctfd-treasure-theme • first blood"},
        "timestamp": now_iso(),
    }


def embed_solve(solve: Dict[str, Any], ch: Dict[str, Any]) -> Dict[str, Any]:
    solver = solver_name(solve)
    return {
        "title": "🏆 Treasure Claimed!",
        "description": f"**{solver}** berhasil solve challenge.",
        "color": 0x2ECC71,
        "fields": [
            {"name": "🏴‍☠️ Solver", "value": solver, "inline": True},
            {"name": "🏝️ Challenge", "value": safe(ch.get("name"), 120), "inline": True},
            {"name": "📦 Category", "value": safe(ch.get("category"), 80), "inline": True},
            {"name": "💰 Points", "value": safe(ch.get("value"), 20), "inline": True},
        ],
        "footer": {"text": "ctfd-treasure-theme • solve"},
        "timestamp": now_iso(),
    }


def process_once(state: Dict[str, Any]) -> Dict[str, Any]:
    runtime["last_poll"] = now_iso()

    seen_challenges = set(map(int, state.get("seen_challenges", [])))
    seen_solves = set(map(int, state.get("seen_solves", [])))
    first_blood = set(map(int, state.get("first_blood_challenges", [])))

    challenges = fetch_challenges()

    for ch in challenges:
        cid = ch.get("id")
        if cid is None:
            continue
        cid = int(cid)

        if cid not in seen_challenges:
            seen_challenges.add(cid)
            if state.get("initialized") and challenge_visible(ch):
                discord_send(embed_new_challenge(ch))
                runtime["new_challenge_sent"] += 1
                log(f"[SEND] New challenge: {ch.get('name')}")

    solves = fetch_solves()
    solves.sort(key=lambda s: str(s.get("date", "")))

    for solve in solves:
        sid = solve_id(solve)
        cid = challenge_id_from_solve(solve)

        if cid is None:
            seen_solves.add(sid)
            continue

        try:
            ch = get_challenge(cid)
        except Exception:
            ch = {"id": cid, "name": f"Challenge #{cid}", "category": "-", "value": "?"}

        if not challenge_visible(ch):
            seen_solves.add(sid)
            continue

        is_new_solve = sid not in seen_solves
        is_first = cid not in first_blood

        if is_first:
            first_blood.add(cid)
            if state.get("initialized"):
                discord_send(embed_first_blood(solve, ch))
                runtime["first_blood_sent"] += 1
                log(f"[SEND] First blood: challenge_id={cid} solve_id={sid}")

        elif ANNOUNCE_SOLVES and is_new_solve and state.get("initialized"):
            discord_send(embed_solve(solve, ch))
            runtime["solve_sent"] += 1
            log(f"[SEND] Solve: challenge_id={cid} solve_id={sid}")

        seen_solves.add(sid)

    if not state.get("initialized"):
        state["initialized"] = True
        if SKIP_EXISTING_ON_FIRST_RUN:
            log("[INIT] Existing challenges/solves disimpan tanpa announce. Buat event baru untuk test.")

    state["seen_challenges"] = sorted(seen_challenges)
    state["seen_solves"] = sorted(seen_solves)
    state["first_blood_challenges"] = sorted(first_blood)
    return state


def check_ctfd() -> Dict[str, Any]:
    challenges = fetch_challenges()
    solves = fetch_solves()
    runtime["last_ctfd_check"] = now_iso()
    return {
        "ok": True,
        "ctfd_url": CTFD_URL,
        "challenges": len(challenges),
        "solves": len(solves),
        "first_challenge": challenges[0] if challenges else None,
        "first_solve": solves[0] if solves else None,
    }


class Handler(BaseHTTPRequestHandler):
    def json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path in ("/", "/health"):
                self.json(200, {"ok": True, "service": "ctfd-discord-treasure-bot", "runtime": runtime})
                return

            if path == "/status":
                self.json(200, {"ok": True, "runtime": runtime, "state": load_state()})
                return

            if path == "/test-discord":
                discord_send(embed_test())
                runtime["last_discord_test"] = now_iso()
                self.json(200, {"ok": True, "message": "Discord test sent"})
                return

            if path == "/test-ctfd":
                self.json(200, check_ctfd())
                return

            if path == "/force-poll":
                state = process_once(load_state())
                save_state(state)
                self.json(200, {"ok": True, "message": "Poll selesai", "state": state})
                return

            self.json(404, {"ok": False, "routes": ["/health", "/status", "/test-discord", "/test-ctfd", "/force-poll"]})
        except Exception as exc:
            runtime["last_error"] = str(exc)
            self.json(500, {"ok": False, "error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def start_http() -> None:
    server = ThreadingHTTPServer((BOT_HOST, BOT_PORT), Handler)
    log(f"[HTTP] listening on {BOT_HOST}:{BOT_PORT}")
    server.serve_forever()


def validate() -> None:
    missing = [k for k, v in {
        "CTFD_URL": CTFD_URL,
        "CTFD_TOKEN": CTFD_TOKEN,
        "DISCORD_WEBHOOK_URL": DISCORD_WEBHOOK_URL,
    }.items() if not v]
    if missing:
        raise SystemExit(f"Missing env: {', '.join(missing)}")


def main() -> None:
    validate()
    runtime["started_at"] = now_iso()
    threading.Thread(target=start_http, daemon=True).start()

    state = load_state()
    log("[BOT] started")
    log(f"[BOT] CTFd: {CTFD_URL}")
    log(f"[BOT] Poll interval: {POLL_INTERVAL}s")

    while True:
        try:
            state = process_once(state)
            save_state(state)
            runtime["last_error"] = None
        except Exception as exc:
            runtime["last_error"] = str(exc)
            log(f"[ERROR] {exc}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
