"""Remote control from the public website.

The Mac opens an encrypted Cloudflare quick tunnel to this server. The website finds the tunnel address in the
published data and, after you enter the passcode, sends the same API calls the local app does.

Security rules:
- Requests from this Mac (127.0.0.1) need no passcode. Anything arriving through the tunnel does.
- Logging in with the passcode returns a random token (valid 30 days); only its hash is stored.
- Too many wrong passcodes lock logins for 15 minutes.
- Only the StockBot website origin may call the API from a browser (CORS).
- The passcode itself can only be viewed or changed from this Mac.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time

TOKEN_DAYS = 30
MAX_FAILS, LOCK_SECONDS = 5, 900
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class Remote:
    def __init__(self, data_dir, port, on_url_change):
        self.path = os.path.join(data_dir, "remote.json")
        self.port = port
        self.on_url_change = on_url_change
        self.lock = threading.RLock()
        self.state = {"enabled": True, "passcode": None, "tokens": {}}
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.state.update(json.load(f))
        if not self.state["passcode"]:
            self.state["passcode"] = _new_passcode()
            self._save()
        self.url = None
        self.proc = None
        self.fails = []
        self._stop = threading.Event()

    def _save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    # ---------- auth ----------
    @staticmethod
    def is_remote(headers):
        host = headers.get("host", "").split(":")[0]
        return "cf-connecting-ip" in headers or host not in ("127.0.0.1", "localhost")

    def login(self, passcode, client):
        now = time.time()
        with self.lock:
            self.fails = [f for f in self.fails if now - f < LOCK_SECONDS]
            if len(self.fails) >= MAX_FAILS:
                return None, "Too many wrong passcodes. Try again in 15 minutes."
            if not self.state["enabled"]:
                return None, "Remote control is turned off on the StockBot computer."
            if not hmac.compare_digest(_norm(passcode), _norm(self.state["passcode"])):
                self.fails.append(now)
                return None, "Wrong passcode."
            token = secrets.token_urlsafe(32)
            self.state["tokens"][_hash(token)] = {"expires": now + TOKEN_DAYS * 86400, "created": now, "client": client[:120]}
            self._save()
        return token, None

    def check(self, auth_header):
        if not self.state["enabled"] or not auth_header or not auth_header.startswith("Bearer "):
            return False
        info = self.state["tokens"].get(_hash(auth_header[7:]))
        return bool(info and info["expires"] > time.time())

    def logout(self, auth_header):
        if auth_header and auth_header.startswith("Bearer "):
            with self.lock:
                self.state["tokens"].pop(_hash(auth_header[7:]), None)
                self._save()

    def config(self):
        now = time.time()
        return {"enabled": self.state["enabled"], "url": self.url, "tunnel_running": bool(self.proc and self.proc.poll() is None),
                "cloudflared_installed": bool(_cloudflared()), "passcode": self.state["passcode"],
                "devices": sum(1 for t in self.state["tokens"].values() if t["expires"] > now)}

    def update(self, body):
        with self.lock:
            if "passcode" in body and body["passcode"] is not None:
                p = str(body["passcode"]).strip().upper()
                if len(p) < 8:
                    raise ValueError("Passcode must be at least 8 characters.")
                self.state["passcode"] = p
                self.state["tokens"] = {}  # new passcode signs out every device
            if body.get("new_passcode"):
                self.state["passcode"], self.state["tokens"] = _new_passcode(), {}
            if body.get("revoke_all"):
                self.state["tokens"] = {}
            if "enabled" in body:
                self.state["enabled"] = bool(body["enabled"])
            self._save()
        if "enabled" in body:
            (self.start if self.state["enabled"] else self.stop)()
        return self.config()

    # ---------- tunnel ----------
    def start(self):
        if not self.state["enabled"] or not _cloudflared():
            return
        if self.proc and self.proc.poll() is None:
            return
        self._stop.clear()
        threading.Thread(target=self._run_tunnel, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self._set_url(None)

    def _run_tunnel(self):
        while not self._stop.is_set():
            self.proc = subprocess.Popen(
                [_cloudflared(), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            for line in self.proc.stderr:
                m = URL_RE.search(line)
                if m:
                    self._set_url(m.group(0))
            self.proc.wait()
            self._set_url(None)
            self._stop.wait(30)  # tunnel dropped: retry

    def _set_url(self, url):
        if url != self.url:
            self.url = url
            if url:
                threading.Thread(target=self.on_url_change, daemon=True).start()


def _cloudflared():
    return shutil.which("cloudflared") or next((p for p in ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared")
                                                if os.path.exists(p)), None)


def _new_passcode():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alike characters
    raw = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def _norm(p):
    return re.sub(r"[\s-]", "", str(p or "")).upper()


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()
