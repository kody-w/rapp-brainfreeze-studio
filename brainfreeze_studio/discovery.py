"""The Power Platform environments a person can reach, from the Dataverse Global Discovery Service, asked with their
own token. The same list the Azure Function's sign-in page shows (examples/azure-function/auth.py), for a person
signed in with the Azure CLI instead."""
import json
import re
import urllib.error
import urllib.request

DISCOVERY = "https://globaldisco.crm.dynamics.com/"
ENV_URL = re.compile(r"^https://[a-z0-9-]+\.crm[0-9]*\.dynamics\.com/?$")
# Global Discovery's OrganizationType; 5, 12 and 13 checked against the Power Platform admin API's environmentSku
KINDS = {0: "Production", 5: "Sandbox", 6: "Trial", 12: "Default", 13: "Developer", 14: "Trial", 15: "Teams"}


class DiscoveryError(RuntimeError):
    pass


def environments(token, opener=urllib.request.urlopen):
    """name, url, id, kind and region of each enabled environment the token's owner belongs to, sorted by name."""
    req = urllib.request.Request(DISCOVERY + "api/discovery/v2.0/Instances",
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with opener(req, timeout=60) as r:
            rows = json.loads(r.read().decode("utf-8") or "{}").get("value") or []
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise DiscoveryError(f"the Global Discovery Service did not accept this sign-in (HTTP {e.code}); "
                                 "sign in again with az login")
        raise DiscoveryError(f"the Global Discovery Service answered HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise DiscoveryError(f"could not reach the Global Discovery Service: {e}")
    found = []
    for row in rows:
        url = str(row.get("Url") or "").rstrip("/") + "/"
        if row.get("State", 0) != 0 or not ENV_URL.match(url):
            continue
        found.append({"name": row.get("FriendlyName") or row.get("UniqueName") or url, "url": url,
                      "id": row.get("EnvironmentId"), "kind": KINDS.get(row.get("OrganizationType"), ""),
                      "region": row.get("Region") or ""})
    return sorted(found, key=lambda env: (env["name"].casefold(), env["url"]))
