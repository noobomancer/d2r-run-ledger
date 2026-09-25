"""D2R run tracker -- times Diablo II: Resurrected runs from the outside.

Never touches the game process. It reads the Windows TCP connection table
(the same data `netstat` shows) and watches for D2R's game-server connection:
one opens when you enter a game and closes when you leave.

Classifier (derived from ~70 logged games, 2026-09-23/24):
  game servers   35.228.x, 34.88.x, 37.244.50.x on :443, strictly sequential
  ignored        137.221.x   Blizzard lobby / matchmaking, churns mid-game
                 66.40.x, :1119   Battle.net services, open all session
                 34.117.x    background service, overlaps games
                 3.x         AWS CloudFront (patch / CDN)
                 < MIN_AGE s launch-time region ping probes (all ~1 s)

Usage:  py tracker.py [--port 8777] [--host 127.0.0.1] [--hotkey F9]
"""

import argparse
import ctypes
import json
import os
import socket
import struct
import subprocess
import threading
import time
import uuid
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "history.json")

DENY_PREFIXES = ("137.221.", "66.40.", "34.117.", "3.", "127.", "0.")
GAME_PORTS = {443}
MIN_AGE = 5              # seconds a connection must live before it counts as a run
POLL = 1.0               # seconds between connection-table reads
NEW_SESSION_GAP = 2 * 3600   # idle this long on startup -> start a fresh session

# ---------------------------------------------------------------------------
# Windows TCP table via iphlpapi (no psutil, no subprocess per poll)

_iphlp = ctypes.WinDLL("iphlpapi")
AF_INET, TCP_TABLE_OWNER_PID_ALL, MIB_TCP_STATE_ESTAB = 2, 5, 5


class _Row(ctypes.Structure):
    _fields_ = [("state", wintypes.DWORD), ("laddr", wintypes.DWORD),
                ("lport", wintypes.DWORD), ("raddr", wintypes.DWORD),
                ("rport", wintypes.DWORD), ("pid", wintypes.DWORD)]


def tcp_connections(pid):
    """Established IPv4 connections owned by pid -> set of (ip, rport, lport)."""
    size = wintypes.DWORD(0)
    _iphlp.GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET,
                               TCP_TABLE_OWNER_PID_ALL, 0)
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = wintypes.DWORD(len(buf))
        if _iphlp.GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET,
                                      TCP_TABLE_OWNER_PID_ALL, 0) == 0:
            break
    else:
        return set()
    n = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
    rows = ctypes.cast(ctypes.addressof(buf) + 4, ctypes.POINTER(_Row * n)).contents
    out = set()
    for r in rows:
        if r.pid != pid or r.state != MIB_TCP_STATE_ESTAB:
            continue
        ip = socket.inet_ntoa(struct.pack("<I", r.raddr))
        out.add((ip, socket.ntohs(r.rport & 0xFFFF), socket.ntohs(r.lport & 0xFFFF)))
    return out


def find_d2r_pid():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq D2R.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, creationflags=0x08000000).stdout
        if "D2R.exe" in out:
            return int(out.split('","')[1])
    except Exception:
        pass
    return None


def is_candidate(conn):
    ip, port, _ = conn
    return port in GAME_PORTS and not ip.startswith(DENY_PREFIXES)

# ---------------------------------------------------------------------------
# State


class Tracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.history = self._load()
        self.pid = None
        self.active = None          # connection key of the run in progress
        self.first_seen = {}        # candidate key -> epoch first observed
        self.ignored = set()        # overlapping candidates, never promoted
        self.boot_conns = None      # connections already open when the tracker started
        self._maybe_new_session_on_startup()

    # -- persistence --
    def _load(self):
        try:
            with open(DATA, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {"sessions": []}

    def _save(self):
        tmp = DATA + ".tmp"
        os.makedirs(os.path.dirname(DATA), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.history, f, indent=1)
        os.replace(tmp, DATA)

    def _session(self):
        return self.history["sessions"][-1]

    def _new_session(self):
        self.history["sessions"].append(
            {"id": uuid.uuid4().hex[:8], "start": time.time(), "runs": []})

    def _maybe_new_session_on_startup(self):
        s = self.history["sessions"]
        # A run left open by a previous crash/exit is closed at its last save.
        if s and s[-1]["runs"] and s[-1]["runs"][-1].get("end") is None:
            r = s[-1]["runs"][-1]
            r["end"] = r.get("seen", r["start"])
        last = 0
        if s:
            last = max([s[-1]["start"]] + [r["end"] or r["start"] for r in s[-1]["runs"]])
        if not s or s[-1].get("stopped") or time.time() - last > NEW_SESSION_GAP:
            self._new_session()
        self._save()

    # -- detection loop --
    def poll_forever(self):
        pid_check = 0
        while True:
            now = time.time()
            if now - pid_check > 5 or self.pid is None:
                self.pid = find_d2r_pid()
                pid_check = now
            conns = {c for c in tcp_connections(self.pid) if is_candidate(c)} if self.pid else set()
            with self.lock:
                self._step(conns, now)
            time.sleep(POLL)

    def _step(self, conns, now):
        if self.boot_conns is None:
            self.boot_conns = set(conns)
            for c in conns:                     # already in a game: count it now, not in 5 s
                self.first_seen[c] = now - MIN_AGE
        for c in conns:
            self.first_seen.setdefault(c, now)
        for c in list(self.first_seen):
            if c not in conns:
                del self.first_seen[c]
                self.ignored.discard(c)

        if self._session().get("stopped"):
            return                                # stopped: watch, but record nothing
        runs = self._session()["runs"]
        if self.active is not None:
            run = runs[-1] if runs else None
            if self.active not in conns:              # left the game
                if run and run.get("end") is None:
                    run["end"] = now
                self.active = None
                self._save()
            else:
                if run is not None:
                    run["seen"] = now
                # Anything else that shows up now is overlap noise.
                self.ignored.update(c for c in conns if c != self.active)
                return

        ready = [c for c in conns if c not in self.ignored and now - self.first_seen[c] >= MIN_AGE]
        if ready:
            c = min(ready, key=lambda k: self.first_seen[k])
            self.active = c
            self.ignored.update(x for x in conns if x != c)
            run = {"id": uuid.uuid4().hex[:8], "start": self.first_seen[c],
                   "end": None, "seen": now, "server": c[0], "drops": []}
            if c in self.boot_conns:
                run["start"] = now          # true start unknown
                run["partial"] = True
            runs.append(run)
            self._save()

    # -- user actions --
    def add_drop(self, name, run_id=None):
        name = name.strip()[:80]
        if not name:
            return False
        with self.lock:
            runs = self._session()["runs"]
            target = None
            if run_id:
                target = next((r for r in runs if r["id"] == run_id), None)
            elif runs:
                target = runs[-1]
            if target is None:
                return False
            target["drops"].append({"id": uuid.uuid4().hex[:8], "t": time.time(), "name": name})
            self._save()
            return True

    def add_herald(self, delta=1, run_id=None):
        """Herald kill counter on a run: the given one, else the current/last run."""
        with self.lock:
            runs = self._session()["runs"]
            if not runs:
                return None
            if run_id is None and self._session().get("stopped"):
                return None                       # F9 after Stop: nothing to count on
            r = next((x for x in runs if x["id"] == run_id), None) if run_id else runs[-1]
            if r is None:
                return None
            r["heralds"] = max(0, r.get("heralds", 0) + int(delta))
            self._save()
            return r["heralds"]

    def delete_drop(self, drop_id):
        with self.lock:
            for r in self._session()["runs"]:
                r["drops"] = [d for d in r["drops"] if d["id"] != drop_id]
            self._save()

    def merge_with_previous(self, run_id):
        with self.lock:
            runs = self._session()["runs"]
            i = next((i for i, r in enumerate(runs) if r["id"] == run_id), None)
            if not i:
                return False
            prev, cur = runs[i - 1], runs.pop(i)
            prev["end"] = cur["end"]
            prev["seen"] = cur.get("seen", prev.get("seen"))
            prev["drops"] += cur["drops"]
            prev["heralds"] = prev.get("heralds", 0) + cur.get("heralds", 0)
            self._save()
            return True

    def delete_run(self, run_id):
        with self.lock:
            s = self._session()
            if self.active and s["runs"] and s["runs"][-1]["id"] == run_id:
                return False                     # don't delete the live run
            s["runs"] = [r for r in s["runs"] if r["id"] != run_id]
            self._save()
            return True

    def stop_session(self):
        """Freeze the session: end any run now and record no more until a new session."""
        with self.lock:
            s = self._session()
            if s.get("stopped"):
                return True
            now = time.time()
            if self.active is not None:
                if s["runs"] and s["runs"][-1].get("end") is None:
                    s["runs"][-1]["end"] = now
                self.ignored.add(self.active)     # don't re-detect the game we're still in
                self.active = None
            s["stopped"] = now
            self._save()
            return True

    def new_session(self):
        """Close the current session. A run in progress moves to the new one."""
        with self.lock:
            old = self._session()
            live = old["runs"].pop() if self.active and old["runs"] else None
            if not old["runs"] and live is None:
                old.pop("stopped", None)          # empty session: just resume it
                old["start"] = time.time()
                self._save()
                return True
            self._new_session()
            if live is not None:
                self._session()["runs"].append(live)
            self._save()
            return True

    def snapshot(self):
        with self.lock:
            s = self._session()
            past = [{"id": x["id"], "start": x["start"], "runs": len(x["runs"]),
                     "drops": sum(len(r["drops"]) for r in x["runs"]),
                     "heralds": sum(r.get("heralds", 0) for r in x["runs"])}
                    for x in self.history["sessions"][:-1][-10:]]
            return {
                "now": time.time(),
                "d2r_running": self.pid is not None,
                "in_game": self.active is not None,
                "session": s,
                "past_sessions": past[::-1],
                # every item name ever logged, newest first -> page suggestions
                "known_items": list(dict.fromkeys(
                    d["name"] for x in reversed(self.history["sessions"])
                    for r in reversed(x["runs"]) for d in reversed(r["drops"]))),
                "diag": {
                    "pid": self.pid,
                    "active": list(self.active) if self.active else None,
                    "candidates": [list(c) + [round(time.time() - t)] for c, t in self.first_seen.items()],
                    "ignored": [list(c) for c in self.ignored],
                },
            }

# ---------------------------------------------------------------------------
# Global hotkey -> +1 herald kill (RegisterHotKey, no hooks). A short beep
# confirms it, since the game has focus and the page isn't visible.

VK = {f"F{i}": 0x6F + i for i in range(1, 13)}


def hotkey_loop(tracker, key):
    import winsound
    user32 = ctypes.WinDLL("user32")
    if not user32.RegisterHotKey(None, 1, 0x4000, VK[key]):   # MOD_NOREPEAT
        print(f"[hotkey] could not register {key} (in use by another app?) -- use the web page instead")
        return
    print(f"[hotkey] {key} = +1 herald kill")
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == 0x0312:   # WM_HOTKEY
            n = tracker.add_herald(1)
            winsound.Beep(880, 70) if n is not None else winsound.Beep(220, 200)

# ---------------------------------------------------------------------------
# HTTP


def make_handler(tracker):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/state":
                return self._json(tracker.snapshot())
            if self.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                p = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            routes = {
                "/api/drop": lambda: tracker.add_drop(p.get("name", ""), p.get("run_id")),
                "/api/herald": lambda: tracker.add_herald(p.get("delta", 1), p.get("run_id")) is not None,
                "/api/session/stop": lambda: tracker.stop_session(),
                "/api/drop/delete":lambda: tracker.delete_drop(p.get("id")) or True,
                "/api/run/merge": lambda: tracker.merge_with_previous(p.get("id")),
                "/api/run/delete": lambda: tracker.delete_run(p.get("id")),
                "/api/session/new": lambda: tracker.new_session(),
            }
            fn = routes.get(self.path)
            if not fn:
                return self._json({"error": "not found"}, 404)
            self._json({"ok": bool(fn())})
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--hotkey", default="F9", choices=list(VK))
    a = ap.parse_args()

    tracker = Tracker()
    threading.Thread(target=tracker.poll_forever, daemon=True).start()
    threading.Thread(target=hotkey_loop, args=(tracker, a.hotkey), daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(tracker))
    print(f"D2R tracker on http://{a.host}:{a.port}   (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
