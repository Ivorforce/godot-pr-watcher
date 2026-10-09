"""Minimal GitHub REST/GraphQL client (stdlib only)."""
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

API = "https://api.github.com"
LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class RateLimited(GitHubError):
    """Primary or secondary rate limit hit. Callers should stop making requests for this run."""


class GitHub:
    def __init__(self, token: str):
        self.token = token

    def _request(self, method: str, url: str, body=None, attempts: int = 4):
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(attempts):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "godot-pr-watcher",
            })
            try:
                with urllib.request.urlopen(req, timeout=60) as res:
                    raw = res.read()
                    return (json.loads(raw) if raw else None), res.headers
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")[:300]
                if e.code in (403, 429) and (e.headers.get("X-RateLimit-Remaining") == "0"
                                             or e.headers.get("Retry-After") or "rate limit" in msg.lower()):
                    raise RateLimited(e.code, msg) from None
                if e.code in (502, 503, 504):
                    log.warning("%s %s -> %d, retrying in %ds", method, url, e.code, 2 ** attempt)
                    time.sleep(2 ** attempt)
                    continue
                raise GitHubError(e.code, msg) from None
            except urllib.error.URLError as e:
                log.warning("%s %s -> %s, retrying", method, url, e.reason)
                time.sleep(2 ** attempt)
        raise GitHubError(0, f"giving up on {method} {url}")

    def rest(self, path: str, params: dict | None = None, method: str = "GET", body=None):
        url = f"{API}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._request(method, url, body)[0]

    def paginate(self, path: str, params: dict | None = None):
        """Yield items across all pages (follows the Link header)."""
        url = f"{API}/{path.lstrip('/')}?" + urllib.parse.urlencode({"per_page": 100, **(params or {})})
        while url:
            items, headers = self._request("GET", url)
            yield from items
            m = LINK_NEXT.search(headers.get("Link") or "")
            url = m.group(1) if m else None

    def graphql(self, query: str, variables: dict | None = None, attempts: int = 4) -> dict:
        """Run a query. Partial results are returned (failed fields are null); errors are logged."""
        res = self._request("POST", f"{API}/graphql", {"query": query, "variables": variables or {}}, attempts)[0]
        if res.get("errors"):
            if any(e.get("type") == "RATE_LIMITED" for e in res["errors"]):
                raise RateLimited(200, json.dumps(res["errors"])[:300])
            log.warning("GraphQL errors: %s", json.dumps(res["errors"])[:500])
        if res.get("data") is None:
            raise GitHubError(200, json.dumps(res.get("errors"))[:300])
        return res["data"]
