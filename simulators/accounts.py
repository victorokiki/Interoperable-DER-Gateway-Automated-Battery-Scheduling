"""
simulators/accounts.py
View-only accounts for people who want to look at the dashboard.

Anyone can create one on the public site; it can look at every page but cannot send commands,
cancel events or use Force Idle. Only the operator login (set with environment variables) can.
Accounts are kept in a Supabase table so they survive the host restarting; the password itself is
never stored, only a salted scrypt hash.

Setup: see supabase/viewer_accounts.sql, then set SUPABASE_URL and SUPABASE_SECRET_KEY on the server.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from werkzeug.security import check_password_hash, generate_password_hash

USERNAME_RE = re.compile(r"^[a-z0-9_-]{3,30}$")
MIN_PASSWORD_LEN, MAX_PASSWORD_LEN = 10, 128
RESERVED = {"operator", "admin", "administrator", "root", "system", "guest", "support", "invinity", "api", "null"}
COMMON_PASSWORDS = {
    "password", "password1", "password123", "1234567890", "12345678910", "qwertyuiop", "iloveyou123",
    "invinity2026", "letmein1234", "welcome1234", "administrator", "abcdefghij", "0123456789",
}
# Hashed once at start-up: checked when a username does not exist, so a wrong username and a wrong
# password take the same time and the login form does not reveal which usernames exist.
_DUMMY_HASH = generate_password_hash("no-such-account-placeholder")


class StoreError(Exception):
    """The account database could not be reached or answered with an error."""


def validate_username(raw: str) -> tuple[str | None, str]:
    """(clean_name, "") or (None, reason)."""
    name = (raw or "").strip().lower()
    if not USERNAME_RE.match(name):
        return None, "Choose a username of 3 to 30 characters: letters, numbers, dash or underscore."
    if name in RESERVED:
        return None, "That username is reserved. Choose another."
    return name, ""


def validate_password(password: str, username: str) -> str:
    """"" when acceptable, otherwise the reason."""
    if len(password or "") < MIN_PASSWORD_LEN:
        return f"Use a password of at least {MIN_PASSWORD_LEN} characters."
    if len(password) > MAX_PASSWORD_LEN:
        return f"Use a password of at most {MAX_PASSWORD_LEN} characters."
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS or username and lowered == username:
        return "That password is too easy to guess. Choose another."
    if len(set(password)) < 5:
        return "That password is too repetitive. Choose another."
    return ""


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Always does one hash comparison, whether or not the account exists."""
    ok = check_password_hash(stored_hash or _DUMMY_HASH, password or "")
    return bool(stored_hash) and ok


class SupabaseStore:
    """The viewer_accounts table, reached through Supabase's REST interface with the server-side
    secret key. The key never leaves the server and the table has no public access (RLS on, no policies)."""

    def __init__(self, url: str, secret_key: str, timeout: float = 6.0):
        self.base = url.rstrip("/") + "/rest/v1/viewer_accounts"
        self.key = secret_key
        self.timeout = timeout

    def _headers(self) -> dict:
        headers = {"apikey": self.key, "Content-Type": "application/json", "Accept": "application/json"}
        if self.key.count(".") == 2:                  # a JWT-style (legacy service_role) key also goes in Authorization
            headers["Authorization"] = f"Bearer {self.key}"
        return headers

    def _call(self, method: str, url: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method, headers={**self._headers(), **({"Prefer": "return=minimal"} if body else {})})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return 409, None
            raise StoreError(f"the account database answered HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            if not re.fullmatch(r"[A-Za-z0-9_.\-]+", self.key):
                # Typically a copy of the masked key (dots) or a stray space or line break from pasting.
                bad = sorted({f"U+{ord(c):04X}" for c in self.key if not re.fullmatch(r"[A-Za-z0-9_.\-]", c)})
                raise StoreError("SUPABASE_SECRET_KEY contains characters a key never has "
                                 f"({', '.join(bad)}; length {len(self.key)}); copy it again from Supabase with its Copy button") from None
            raise StoreError(f"the account database could not be reached ({type(e).__name__})") from None

    def create(self, username: str, password_hash: str) -> bool:
        """True when created, False when the username is already taken."""
        status, _ = self._call("POST", self.base, {"username": username, "password_hash": password_hash})
        return status != 409

    def get_hash(self, username: str) -> str | None:
        query = f"?username=eq.{urllib.parse.quote(username, safe='')}&select=password_hash&limit=1"
        _, rows = self._call("GET", self.base + query)
        return rows[0]["password_hash"] if rows else None


def store_from_environment(environ) -> "SupabaseStore | None":
    url, key = (environ.get("SUPABASE_URL") or "").strip(), re.sub(r"\s+", "", environ.get("SUPABASE_SECRET_KEY") or "")
    return SupabaseStore(url, key) if url.startswith("https://") and key else None
