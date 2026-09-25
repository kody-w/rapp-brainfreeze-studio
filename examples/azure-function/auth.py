"""Who is calling. Every route that does work takes the caller's own delegated Dataverse token and has Dataverse
check it (WhoAmI) before anything else runs, so a token's claims are trusted only after Dataverse accepted the token
for that environment. Decoding a token proves nothing: anyone can write one."""
import base64
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request


def claims(token):
    """A JWT's claims, unverified: only for reading a token that Dataverse checks (or already checked)."""
    try:
        payload = token.split(".")[1]
        found = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (AttributeError, IndexError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def bearer(req):
    header = req.headers.get("Authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


class Verifier:
    """Checks a caller's token: a signed-in user's delegated token for the environment, from an allowed tenant, that
    Dataverse accepts. Accepted tokens are remembered until they expire, so polling a job costs one WhoAmI."""

    def __init__(self, allowed_tenants=(), opener=None, now=time.time):
        self.allowed = {t.strip().lower() for t in allowed_tenants if t and t.strip()}
        self.urlopen = opener or urllib.request.urlopen
        self.now = now
        self._accepted = {}
        self._lock = threading.Lock()

    def __call__(self, token, environment):
        """(claims, WhoAmI) for the caller; PermissionError when the token isn't one to act on."""
        found = claims(token) if token else None
        if not found:
            raise PermissionError("sign in first: send your Dataverse token as Authorization: Bearer <token>")
        if str(found.get("aud", "")).rstrip("/") != environment.rstrip("/"):
            raise PermissionError(f"the token is for {found.get('aud')}, not {environment}")
        if "user_impersonation" not in str(found.get("scp", "")).split():
            raise PermissionError("only a signed-in user's delegated token is accepted, not an app-only token")
        exp = found.get("exp")
        if not isinstance(exp, (int, float)) or exp < self.now() + 60:
            raise PermissionError("the token has expired; sign in again")
        if self.allowed and str(found.get("tid", "")).lower() not in self.allowed:
            raise PermissionError("this deployment serves only its own organizations, and your account's is not one "
                                  "of them")
        key = hashlib.sha256(f"{environment} {token}".encode()).hexdigest()
        with self._lock:
            hit = self._accepted.get(key)
        if hit is None:
            req = urllib.request.Request(environment.rstrip("/") + "/api/data/v9.2/WhoAmI",
                                         headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
            try:
                with self.urlopen(req, timeout=30) as r:
                    who = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                raise PermissionError(f"{environment} did not accept this sign-in (HTTP {e.code}); sign in with an "
                                      "account that can make agents there")
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise PermissionError(f"could not check the sign-in with {environment}: {e}")
            if not isinstance(who, dict) or not who.get("UserId"):
                raise PermissionError(f"{environment} did not say who this sign-in is")
            hit = (exp, who)
            now = self.now()
            with self._lock:
                self._accepted = {k: v for k, v in self._accepted.items() if v[0] > now}
                self._accepted[key] = hit
        return found, hit[1]
