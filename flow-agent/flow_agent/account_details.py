"""Read-only Flow account metadata; credentials stay in the Python process.

Uses the same o30O0e / nzlxg RPCs as flow-go's batchexecute client. Never
uses the legacy aisandbox balance, which can describe a different account.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

import httpx

ORIGIN = "https://flow.google.com"
ENDPOINT = ORIGIN + "/_/AiSandboxAngularFrontend/data/batchexecute"
PROFILE_ARGUMENT = [["me"], [[["person.photo", "person.name", "person.email"]], None, [1, 7]]]


class AccountReadError(Exception):
    """Only a public, fixed diagnostic code; never include upstream bodies."""


def payloads(text: str, rpc: str) -> list:
    values = []
    for line in text.splitlines():
        if not line.startswith("["):
            continue
        try:
            frames = json.loads(line)
        except ValueError:
            continue
        if not isinstance(frames, list):
            continue
        for frame in frames:
            if (isinstance(frame, list) and len(frame) >= 3
                    and frame[:2] == ["wrb.fr", rpc] and isinstance(frame[2], str)):
                try:
                    values.append(json.loads(frame[2]))
                except ValueError:
                    continue
    return values


def profile_identity(doc: object) -> tuple[str, str] | None:
    fields = []

    def walk(value):
        if not isinstance(value, list):
            return
        for entry in value:
            if (isinstance(entry, list) and len(entry) == 1
                    and isinstance(entry[0], list) and len(entry[0]) >= 2
                    and isinstance(entry[0][0], list) and isinstance(entry[0][1], str)):
                fields.append(entry[0][1].strip())
            else:
                walk(entry)

    walk(doc)
    emails = list(dict.fromkeys(s for s in fields if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", s)))
    names = [s for s in fields if s and "@" not in s and not s.startswith(("http:", "https:"))]
    if len(emails) != 1 or not names:
        return None
    return names[0][:128], emails[0][:320]


class FlowAccountReader:
    def __init__(self, client: httpx.AsyncClient, bundle: dict):
        self.client = client
        self.at = str(bundle.get("at") or "")
        self.fsid = str(bundle.get("fsid") or "")
        cookies = bundle.get("cookies")
        if not isinstance(cookies, list):
            raise AccountReadError("incomplete")
        # Domain matching prevents forwarding Labs-only cookies to Flow.
        allowed = [c for c in cookies if isinstance(c, dict)
                   and c.get("domain", "").lstrip(".") in ("google.com", "flow.google.com")
                   and c.get("name") and c.get("value")
                   and c.get("path", "/") == "/"]
        sid = next((c["value"] for c in allowed if c["name"] == "SAPISID"), None)
        if not sid:
            sid = next((c["value"] for c in allowed if c["name"] == "__Secure-3PAPISID"), None)
        if not sid or not self.at or not self.fsid:
            raise AccountReadError("incomplete")
        ts = str(int(time.time()))
        fingerprint = bundle.get("fingerprint") or {}
        self.headers = {
            "Authorization": "SAPISIDHASH " + ts + "_" + hashlib.sha1((ts + " " + sid + " " + ORIGIN).encode()).hexdigest(),
            "Cookie": "; ".join(c["name"] + "=" + c["value"] for c in allowed),
            "Origin": ORIGIN, "Referer": ORIGIN + "/", "X-Same-Domain": "1",
            "User-Agent": fingerprint.get("user_agent") or fingerprint.get("userAgent") or "",
        }

    async def call(self, rpc: str, argument: object) -> list:
        for attempt in range(2):
            try:
                response = await self.client.post(ENDPOINT,
                    params={"rpcids": rpc, "source-path": "/", "f.sid": self.fsid,
                            "rt": "c", "hl": "zh-CN", "_reqid": "101000"},
                    headers=self.headers,
                    data={"f.req": json.dumps([[[rpc, json.dumps(argument), None, "generic"]]]), "at": self.at})
            except (httpx.HTTPError, ValueError):
                raise AccountReadError("network") from None
            if len(response.content) > 2_000_000:
                raise AccountReadError("unread")
            # Decode UTF-8 explicitly; the display name may contain Chinese.
            body = response.content.decode("utf-8", "replace")
            if response.status_code == 400 and attempt == 0:
                token = re.search(r'"xsrf"\s*,\s*"([^"\r\n]+)"', body)
                if token:
                    self.at = token.group(1)
                    continue
            if response.status_code in (401, 403):
                raise AccountReadError("session" if response.status_code == 401 else "refused")
            if response.status_code != 200:
                raise AccountReadError("upstream")
            docs = payloads(body, rpc)
            if not docs:
                raise AccountReadError("unread")
            return docs
        raise AccountReadError("unread")

    async def read(self) -> dict:
        result = {"name": None, "email": None, "credits": None, "error": None}
        # These two reads share the exact account cookie + at + fsid snapshot.
        try:
            docs = await self.call("o30O0e", PROFILE_ARGUMENT)
            identity = next((item for doc in docs if (item := profile_identity(doc))), None)
            if identity:
                result["name"], result["email"] = identity
        except AccountReadError as exc:
            result["error"] = str(exc)
        try:
            docs = await self.call("nzlxg", [])
            for doc in docs:
                if isinstance(doc, list) and doc and type(doc[0]) in (int, float) and doc[0] >= 0 and float(doc[0]).is_integer():
                    result["credits"] = int(doc[0])
                    break
            if result["credits"] is None:
                result["error"] = "unread"
        except AccountReadError as exc:
            result["error"] = str(exc)
        return result
