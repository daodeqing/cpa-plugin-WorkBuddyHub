"""只追加到隔离测试副本。发布 DLL 及上游源码均不含此模拟代码。"""
def install(w, data):
    import base64
    import io
    import json
    import time
    def token(uid, realm):
        def enc(x):
            return base64.urlsafe_b64encode(json.dumps(x).encode()).decode().rstrip("=")
        return enc({"alg": "none"}) + "." + enc({"sub": uid, "uid": uid, "exp": int(time.time()) + 86400,
              "iss": "https://www.workbuddy.ai" if realm == "intl" else "https://codebuddy.cn"}) + ".synthetic"
    def http_json(url, *a, **kw):
        realm = "cn" if "tencent" in url or "workbuddy.cn" in url or "codebuddy.cn" in url else "intl"
        if "/auth/state" in url:
            return {"code": 0, "data": {"state": "upstream-" + realm, "authUrl": "https://auth.example.test/" + realm}}
        if "/auth/token" in url:
            return {"code": 0, "data": {"accessToken": token("synthetic-" + realm, realm),
                    "refreshToken": "synthetic-refresh", "expiresAt": int(time.time()) + 86400}}
        return {"code": 0, "data": {}}
    w.HUB.wb_accounts.http_json = http_json
    w.HUB.urllib.request.urlopen = lambda *a, **kw: io.BytesIO(b'{"data":{"nickname":"Synthetic account"}}')
    w.HUB.fetch_models = lambda realm: [("test-model", {"name": "Test Model", "context_length": 32768})]
    def fetch_credits(account):
        return {"ok": True, "credits": {"remain": 350, "used": 50, "size": 400, "is_paid_user": False}}
    w.HUB.wb_accounts.Account.fetch_credits = fetch_credits
    def open_upstream(payload, **kw):
        account = kw["pin_account"]
        assert account.realm == kw["target_realm"]
        assert kw["allowed_accounts"] == {account.uid}
        assert kw["allow_credit_fallback"] is False
        assert kw["allow_product_switch"] is False
        content = str(payload.get("messages", []))
        if "status429" in content:
            raise w.HUB.RateLimited()
        delta = {"content": "synthetic reply"} if not payload.get("tools") else {
            "tool_calls": [{"index": 0, "id": "call_synthetic", "type": "function",
            "function": {"name": "lookup", "arguments": "{\"x\":1}"}}]}
        first = {"id": "chatcmpl-synthetic", "model": payload["model"],
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        finish = {"id": "chatcmpl-synthetic", "model": payload["model"],
                  "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if payload.get("tools") else "stop"}],
                  "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7,
                            "prompt_tokens_details": {"cached_tokens": 2}}}
        raw = b"data: " + json.dumps(first).encode() + b"\n\n"
        if "truncate" not in content:
            raw += b"data: " + json.dumps(finish).encode() + b"\n\ndata: [DONE]\n\n"
        if "slowstream" in content:
            class SlowResponse(io.BytesIO):
                def __init__(self, data):
                    super().__init__(data)
                    self.reads = 0
                def read1(self, size):
                    self.reads += 1
                    if self.reads == 1:
                        first_end = self.getvalue().find(b"\n\n") + 2
                        return self.read(first_end)
                    if self.reads == 2:
                        time.sleep(1.4)
                    return super().read1(size)
            return SlowResponse(raw), account, None
        return io.BytesIO(raw), account, None
    w.HUB.open_upstream = open_upstream
