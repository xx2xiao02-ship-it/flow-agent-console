import json
from urllib.parse import parse_qs

import httpx
import pytest

from flow_agent.account_details import FlowAccountReader, profile_identity

BUNDLE = {"at": "PRIVATE_AT", "fsid": "PRIVATE_SID", "fingerprint": {"user_agent": "Chrome"},
          "cookies": [{"name": "SAPISID", "value": "PRIVATE_AUTH", "domain": ".google.com", "path": "/"},
                      {"name": "labs_session", "value": "PRIVATE_LABS", "domain": "labs.google", "path": "/"}]}


def reply(rpc, doc):
    return ")]}'\n\n" + json.dumps([["wrb.fr", rpc, json.dumps(doc, ensure_ascii=False), None, None, None, "generic"]], ensure_ascii=False)


@pytest.mark.asyncio
async def test_live_protocol_chinese_identity_zero_balance_and_cookie_scope():
    requests = []
    def handle(request):
        requests.append(request)
        rpc = request.url.params["rpcids"]
        envelope = json.loads(parse_qs(request.content.decode())["f.req"][0])
        assert envelope[0][0][0] == rpc
        assert request.url.params["f.sid"] == "PRIVATE_SID"
        assert "PRIVATE_LABS" not in request.headers["cookie"]
        doc = [[[[[True], "孙镜怡"]]], [[[[False], "me@gmail.com"]]]] if rpc == "o30O0e" else [0, 1, 2, None, 0]
        return httpx.Response(200, content=reply(rpc, doc).encode("utf-8"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await FlowAccountReader(client, BUNDLE).read()
    assert result == {"name": "孙镜怡", "email": "me@gmail.com", "credits": 0, "error": None}
    assert len(requests) == 2
    assert "PRIVATE" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,reason", [(401, "SECRET", "session"), (403, "SECRET", "refused"),
    (200, reply("other-rpc", [1050]), "unread"), (200, reply("nzlxg", [None, 2, 1050]), "unread")])
async def test_refused_or_unrecognized_balance_never_becomes_zero(status, body, reason):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, text=body))) as client:
        result = await FlowAccountReader(client, BUNDLE).read()
    assert result["credits"] is None
    assert result["error"] == reason
    assert "SECRET" not in str(result)


def test_ambiguous_profile_and_bare_id_cannot_be_account_names():
    assert profile_identity(["1234567", [[[], "one@gmail.com"]]]) is None
    assert profile_identity([[[[], "姓名"]], [[[], "one@gmail.com"]], [[[], "two@gmail.com"]]]) is None


@pytest.mark.asyncio
async def test_csrf_priming_is_bounded_and_preserves_account_snapshot():
    count = 0
    def handle(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(400, text='["xsrf","PRIVATE_NEW"]')
        assert parse_qs(request.content.decode())["at"] == ["PRIVATE_NEW"]
        return httpx.Response(200, text=reply("nzlxg", [50, 1, 50]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert await FlowAccountReader(client, BUNDLE).call("nzlxg", []) == [[50, 1, 50]]
    assert count == 2
