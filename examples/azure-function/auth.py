"""Who is calling, and where they can make agents. Every route that does work takes the caller's own delegated
Dataverse token and has Dataverse check it (WhoAmI) before anything else runs, so a token's claims are trusted only
after Dataverse accepted the token for that environment. Decoding a token proves nothing: anyone can write one.

A person signs in once, for the Global Discovery Service, which lists the environments they belong to. When they
pick one, the sign-in's refresh token gets a token for that environment (UserToken), and the Function checks that
their roles there let them create what a deploy creates."""
import base64
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DISCOVERY = "https://globaldisco.crm.dynamics.com/"
ENV_URL = re.compile(r"^https://[a-z0-9-]+\.crm[0-9]*\.dynamics\.com/?$")
# Global Discovery's OrganizationType; 5, 12 and 13 checked against the Power Platform admin API's environmentSku
KINDS = {0: "Production", 5: "Sandbox", 6: "Trial", 12: "Default", 13: "Developer", 14: "Trial", 15: "Teams"}
# what a deploy creates, by the privilege it needs
MAKER_RIGHTS = {"prvCreatebot": "agents", "prvCreatebotcomponent": "agent tools and skills",
                "prvCreateWorkflow": "flows", "prvCreateconnectionreference": "connection references",
                "prvCreateEnvironmentVariableDefinition": "environment variables"}


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


def _aad_error(e):
    try:
        detail = json.loads(e.read().decode() or "{}")
    except ValueError:
        detail = {}
    text = detail.get("error_description") or detail.get("error") or f"HTTP {e.code}"
    return text.split(" Trace ID:")[0].strip()[:300]


# Publishing a code app: PowerApps Service's delegated `User` permission, which a user can consent to themselves.
POWERAPPS_SCOPE = "https://service.powerapps.com//User offline_access"
POWERAPPS_AUDIENCES = ("https://service.powerapps.com", "475226c6-020e-4fb2-8a90-7a972cbfc1d4")
# Making the user's own SharePoint connection when they have none (API Hub's first-party login exchanges this token
# on their behalf). Optional: without it a deploy uses a SharePoint connection the user already has.
APIHUB_SCOPE = "https://apihub.azure.com/.default offline_access"


class ConsentRequired(PermissionError):
    """The user hasn't let this app act for them on that service yet: a sign-in that asks for it fixes that."""


class UserToken:
    """The user's delegated token for one environment's Dataverse (or, with `scope`, another service), got or
    refreshed with their sign-in's refresh token through the public client when it nears expiry. With no access
    token, the first call gets one (how a sign-in reaches the environment the user picks). Called before every
    request."""

    def __init__(self, access_token, environment, refresh_token=None, client_id=None, tenant="organizations",
                 opener=None, now=time.time, scope=None):
        self.access_token, self.refresh_token = access_token, refresh_token
        self.environment, self.client_id, self.tenant, self.scope = environment, client_id, tenant, scope
        self.urlopen = opener or urllib.request.urlopen
        self.now = now
        self.refreshed = 0

    def expires(self):
        return (claims(self.access_token) or {}).get("exp", 0)

    def __call__(self):
        if self.expires() - self.now() > 300:
            return self.access_token
        if not (self.refresh_token and self.client_id):
            if self.expires() > self.now():
                return self.access_token
            raise PermissionError("your sign-in expired while the job ran; sign in again and rerun it (a deploy "
                                  "picks up where it stopped)")
        data = urllib.parse.urlencode({"grant_type": "refresh_token", "client_id": self.client_id,
                                       "refresh_token": self.refresh_token,
                                       "scope": self.scope or f"{self.environment}user_impersonation offline_access"}).encode()
        req = urllib.request.Request(f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token", data=data,
                                     method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self.urlopen(req, timeout=30) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            why = _aad_error(e)
            if "AADSTS65001" in why or "consent" in why.lower():
                raise ConsentRequired(f"you haven't allowed this app to do that for you yet ({why})")
            raise PermissionError(f"could not refresh your sign-in; sign in again ({why})")
        self.access_token = body["access_token"]
        self.refresh_token = body.get("refresh_token") or self.refresh_token
        self.refreshed += 1
        return self.access_token


class Verifier:
    """Checks a caller's token: a signed-in user's delegated token for the environment, from an allowed tenant, that
    Dataverse accepts. Accepted tokens are remembered until they expire, so polling a job costs one WhoAmI."""

    def __init__(self, allowed_tenants=(), opener=None, now=time.time):
        self.allowed = {t.strip().lower() for t in allowed_tenants if t and t.strip()}
        self.urlopen = opener or urllib.request.urlopen
        self.now = now
        self._accepted = {}
        self._lock = threading.Lock()

    def _checked(self, token, audience):
        """The token's claims if it is a signed-in user's delegated token for this audience, from an allowed tenant,
        and not expired. Unverified: whoever the token is for (Dataverse, Global Discovery) verifies it."""
        found = claims(token) if token else None
        if not found:
            raise PermissionError("sign in first: send your Dataverse token as Authorization: Bearer <token>")
        if str(found.get("aud", "")).rstrip("/") != audience.rstrip("/"):
            raise PermissionError(f"the token is for {found.get('aud')}, not {audience}")
        if "user_impersonation" not in str(found.get("scp", "")).split():
            raise PermissionError("only a signed-in user's delegated token is accepted, not an app-only token")
        exp = found.get("exp")
        if not isinstance(exp, (int, float)) or exp < self.now() + 60:
            raise PermissionError("the token has expired; sign in again")
        if self.allowed and str(found.get("tid", "")).lower() not in self.allowed:
            raise PermissionError("this deployment serves only its own organizations, and your account's is not one "
                                  "of them")
        return found

    def _get(self, url, token):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        with self.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def environments(self, token):
        """The environments the signed-in user belongs to, from the Global Discovery Service with their own token
        (which it verifies): name, URL, kind and region, sorted by name. Disabled ones are left out."""
        self._checked(token, DISCOVERY)
        try:
            rows = self._get(DISCOVERY + "api/discovery/v2.0/Instances", token).get("value") or []
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise PermissionError(f"the Global Discovery Service did not accept this sign-in (HTTP {e.code}); "
                                      "sign in again")
            raise RuntimeError(f"the Global Discovery Service answered HTTP {e.code}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise RuntimeError(f"could not reach the Global Discovery Service: {e}")
        found = []
        for row in rows:
            url = str(row.get("Url") or "").rstrip("/") + "/"
            if row.get("State", 0) != 0 or not ENV_URL.match(url):
                continue
            found.append({"name": row.get("FriendlyName") or row.get("UniqueName") or url, "url": url,
                          "id": row.get("EnvironmentId"), "kind": KINDS.get(row.get("OrganizationType"), ""),
                          "region": row.get("Region") or ""})
        return sorted(found, key=lambda env: (env["name"].casefold(), env["url"]))

    def missing_rights(self, token, environment, user_id):
        """What this user's roles in the environment don't let them create, of what a deploy creates (empty when
        they can deploy there). A privilege this environment doesn't have isn't counted. RuntimeError when the roles
        can't be read."""
        def lacks(privilege):
            url = (f"{environment.rstrip('/')}/api/data/v9.2/systemusers({user_id})/Microsoft.Dynamics.CRM."
                   f"RetrieveUserPrivilegeByPrivilegeName(PrivilegeName='{privilege}')")
            try:
                return not self._get(url, token).get("RolePrivileges")
            except urllib.error.HTTPError as e:
                if e.code == 400:
                    return False
                raise RuntimeError(f"could not read your roles in {environment} (HTTP {e.code})")
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise RuntimeError(f"could not read your roles in {environment}: {e}")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", str(user_id)):
            raise RuntimeError(f"{environment} did not say who this sign-in is")
        with ThreadPoolExecutor(len(MAKER_RIGHTS)) as pool:
            lacking = list(pool.map(lacks, MAKER_RIGHTS))
        return [what for (_, what), no in zip(MAKER_RIGHTS.items(), lacking) if no]

    def __call__(self, token, environment):
        """(claims, WhoAmI) for the caller; PermissionError when the token isn't one to act on."""
        found = self._checked(token, environment)
        exp = found["exp"]
        key = hashlib.sha256(f"{environment} {token}".encode()).hexdigest()
        with self._lock:
            hit = self._accepted.get(key)
        if hit is None:
            try:
                who = self._get(environment.rstrip("/") + "/api/data/v9.2/WhoAmI", token)
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
