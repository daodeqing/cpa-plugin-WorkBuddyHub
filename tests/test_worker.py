"""只用虚构凭据和模拟上游，验证原版模块与适配层的真实调用链。"""
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import worker as w
RUN = ROOT / ".work" / ("unit-" + uuid.uuid4().hex)


def token(uid, realm):
    def enc(v):
        return base64.urlsafe_b64encode(json.dumps(v).encode()).rstrip(b"=").decode()
    return enc({"alg": "none"}) + "." + enc({"sub": uid, "uid": uid, "exp": int(time.time()) + 86400,
           "iss": "https://www.workbuddy.ai" if realm == "intl" else "https://codebuddy.cn"}) + ".synthetic"


def account(uid="synthetic-user", realm="intl"):
    a = w.HUB.wb_accounts.Account({"uid": uid, "nickname": "Test " + realm,
        "realm": realm, "accessToken": token(uid, realm), "refreshToken": "synthetic-refresh",
        "expiresAt": int(time.time()) + 86400, "enabled": True, "tasksEnabled": False})
    w.POOLS[realm].add(a)
    return a


def request(a, model=None):
    ref = w.reference(a)
    model = model or "workbuddy-" + a.realm + "/test-model"
    payload = {"model": model, "messages": [{"role": "user", "content": "synthetic"}],
               "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]}
    return {"StorageJSON": w.b64(json.dumps(ref).encode()), "Model": model,
            "Payload": w.b64(json.dumps(payload).encode())}


def stream(parts, terminal=True):
    chunks = []
    for p in parts:
        chunks.append(b"data: " + json.dumps(p, ensure_ascii=False).encode() + b"\n\n")
    if terminal:
        chunks.append(b"data: [DONE]\n\n")
    return io.BytesIO(b"".join(chunks))


def sample(tool=False):
    delta = {"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
              "function": {"name": "lookup", "arguments": "{\"x\":1}"}}]} if tool else {"content": "hello error world"}
    return [{"id": "chatcmpl-test", "model": "test-model", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": "chatcmpl-test", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool else "stop"}],
             "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7,
                       "prompt_tokens_details": {"cached_tokens": 2}}}]


class WorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        w.bootstrap(RUN)
        cls.hub = w.HUB

    def setUp(self):
        w.SESSIONS.clear()
        for p in w.POOLS.values():
            p.accounts = []
            p.logins = {}

    def test_bootstrap_has_no_server_or_scheduler(self):
        self.assertIsNone(getattr(w.HUB, "SCHEDULER", None))
        self.assertFalse(any(t.name.startswith("ThreadingHTTP") for t in __import__("threading").enumerate()))

    def test_login_start_uses_same_origin_and_no_network(self):
        result = w.login_start({"BaseURL": "http://127.0.0.1:18317/v0/management"})
        self.assertTrue(result["URL"].startswith("http://127.0.0.1:18317/v0/resource/plugins/workbuddy-hub/login?"))
        self.assertEqual(w.login_poll({"State": result["State"]}), {"Status": "pending"})
        page = w.resource({"Method": "GET", "Query": {"ticket": [result["State"]]}})
        self.assertIn("国际版", base64.b64decode(page["Body"]).decode())
        self.assertNotIn("synthetic-refresh", base64.b64decode(page["Body"]).decode())

    def test_login_bad_base_rejected(self):
        for base in ("", "file:///tmp/a", "http://user:secret@localhost"):
            with self.assertRaises(w.BridgeError):
                w.login_start({"BaseURL": base})

    def test_cross_site_and_invalid_ticket_rejected(self):
        state = w.login_start({"BaseURL": "http://127.0.0.1:18317"})["State"]
        with self.assertRaises(w.BridgeError):
            w.resource({"Query": {"ticket": [state]}, "Headers": {"Origin": ["https://foreign.test"]}})
        with self.assertRaises(w.BridgeError):
            w.resource({"Query": {"ticket": ["invalid"]}})

    def login(self, realm):
        started = w.login_start({"BaseURL": "http://127.0.0.1:18317"})
        uid = "same-uid"
        def fake_json(url, *a, **kw):
            if "/auth/state" in url:
                return {"code": 0, "data": {"state": "upstream-" + realm, "authUrl": "https://auth.example.test/" + realm}}
            if "/auth/token" in url:
                return {"code": 0, "data": {"accessToken": token(uid, realm), "refreshToken": "synthetic-refresh",
                        "expiresAt": int(time.time()) + 3600}}
            return {"code": 0, "data": {}}
        with patch.object(w.HUB.wb_accounts, "http_json", side_effect=fake_json), \
             patch.object(w.HUB.urllib.request, "urlopen", return_value=io.BytesIO(b'{"data":{"nickname":"synthetic"}}')):
            response = w.resource({"Method": "GET", "Query": {"ticket": [started["State"]], "realm": [realm]}})
            self.assertEqual(response["StatusCode"], 303)
            result = w.login_poll({"State": started["State"]})
            self.assertEqual(result["Status"], "success")
            self.assertEqual(w.login_poll({"State": started["State"]})["Status"], "success")
        return result["Auth"]

    def test_real_upstream_oauth_intl(self):
        auth = self.login("intl")
        ref = w.parse_ref(auth["StorageJSON"])
        self.assertEqual(ref["realm"], "intl")
        self.assertNotIn("accessToken", ref)
        self.assertNotIn("refreshToken", ref)
        self.assertEqual(auth["Provider"], "workbuddy-hub")

    def test_real_upstream_oauth_cn(self):
        auth = self.login("cn")
        self.assertEqual(w.parse_ref(auth["StorageJSON"])["realm"], "cn")

    def test_regions_same_uid_persist_separately(self):
        a = account("same-uid", "intl")
        b = account("same-uid", "cn")
        self.assertNotEqual(a.path, b.path)
        w.POOLS["intl"].load()
        w.POOLS["cn"].load()
        self.assertEqual(w.POOLS["intl"].get("same-uid").realm, "intl")
        self.assertEqual(w.POOLS["cn"].get("same-uid").realm, "cn")

    def test_cancel_and_expire_never_complete(self):
        s = w.login_start({"BaseURL": "http://localhost:18317"})["State"]
        w.cancel({"State": s})
        with self.assertRaises(w.BridgeError):
            w.login_poll({"State": s})
        s = w.login_start({"BaseURL": "http://localhost:18317"})["State"]
        w.SESSIONS[s]["expires"] = time.time() - 1
        with self.assertRaises(w.BridgeError):
            w.login_poll({"State": s})

    def test_duplicate_region_choice_rejected(self):
        s = w.login_start({"BaseURL": "http://localhost:18317"})["State"]
        with patch.object(w.POOLS["intl"], "start_login", return_value={"state": "a", "authUrl": "https://auth.example.test"}):
            w.choose(s, "intl")
            with self.assertRaises(w.BridgeError):
                w.choose(s, "cn")

    def test_same_region_choice_reuses_pending_authorization(self):
        state = w.login_start({"BaseURL": "http://localhost:18317"})["State"]
        with patch.object(w.POOLS["intl"], "start_login", return_value={
                "state": "synthetic-state", "authUrl": "https://auth.example.test/intl"}) as start:
            original = w.choose(state, "intl")
            self.assertEqual(w.choose(state, "intl"), original)
            w.SESSIONS[state]["phase"] = "polling"
            self.assertEqual(w.choose(state, "intl"), original)
            self.assertEqual(start.call_count, 1)

    def test_region_selection_uses_navigation_links(self):
        state = w.login_start({"BaseURL": "http://localhost:18317"})["State"]
        page = w.resource({"Method": "GET", "Query": {"ticket": [state]}})
        html = base64.b64decode(page["Body"]).decode()
        self.assertNotIn("<form", html)
        self.assertIn('href="?ticket=' + state + '&amp;realm=intl"', html)
        self.assertIn('href="?ticket=' + state + '&amp;realm=cn"', html)

    def test_parser_refuses_other_plugins(self):
        for provider in ("workbuddy", "gflow", "codex"):
            blob = {"type": provider, "uid": "u", "realm": "intl"}
            self.assertEqual(w.dispatch("auth.parse", {"RawJSON": w.b64(json.dumps(blob).encode())}), {"Handled": False})

    def test_parser_preserves_disabled_and_filename(self):
        ref = {"type": "workbuddy-hub", "uid": "u", "realm": "intl", "disabled": True}
        result = w.dispatch("auth.parse", {"RawJSON": w.b64(json.dumps(ref).encode()), "FileName": "original.json"})
        self.assertEqual(result["Auth"]["FileName"], "original.json")
        self.assertTrue(result["Auth"]["Disabled"])
        self.assertEqual(result["Auth"]["ID"], "")

    def test_refresh_uses_original_account_method(self):
        a = account()
        refreshed = []
        def fake_refresh():
            a.access_token = token(a.uid, a.realm)
            a.save(w.POOLS[a.realm].dir)
            refreshed.append(True)
        with patch.object(a, "refresh", side_effect=fake_refresh):
            result = w.dispatch("auth.refresh", request(a))
        self.assertEqual(refreshed, [True])
        self.assertEqual(result["Auth"]["FileName"], "")
        self.assertNotIn("accessToken", w.parse_ref(result["Auth"]["StorageJSON"]))

    def test_original_refresh_http_and_persistence(self):
        a = account()
        req = request(a)
        with patch.object(w.HUB.wb_accounts, "http_json", return_value={"code": 0, "data": {
            "accessToken": token(a.uid, a.realm), "refreshToken": "synthetic-rotated"}}):
            result = w.dispatch("auth.refresh", req)
        self.assertEqual(a.refresh_token, "synthetic-rotated")
        self.assertEqual(json.loads(Path(a.path).read_text())["refreshToken"], "synthetic-rotated")
        self.assertEqual(result["Auth"]["FileName"], "")

    def test_original_refresh_failure_is_not_success(self):
        a = account()
        with patch.object(w.HUB.wb_accounts, "http_json", return_value={"code": 0, "data": {}}):
            with self.assertRaises(w.BridgeError):
                w.dispatch("auth.refresh", request(a))

    def test_disabled_auth_never_executes(self):
        a = account()
        req = request(a)
        req["AuthMetadata"] = {"disabled": True}
        with patch.object(w.HUB, "open_upstream") as fn:
            with self.assertRaises(w.BridgeError):
                w.execute(req)
            fn.assert_not_called()

    def test_cross_region_model_never_executes(self):
        a = account()
        with patch.object(w.HUB, "open_upstream") as fn:
            with self.assertRaises(w.BridgeError):
                w.execute(request(a, "workbuddy-cn/test-model"))
            fn.assert_not_called()

    def test_original_bundled_catalog_registers_models(self):
        selected = account()
        with patch.dict(w.HUB._models_cache, {}, clear=True), \
             patch.object(w.HUB, "curated_live_sources", return_value=([], False, "bundled")), \
             patch.object(w.HUB, "fetch_endpoint_models", return_value=[]):
            response = w.dispatch("model.for_auth", request(selected))
        self.assertGreater(len(response["Models"]), 0)
        self.assertTrue(all(model["ID"].startswith("workbuddy-intl/") for model in response["Models"]))

    def test_quota_uses_selected_account_and_normalized_abi(self):
        for realm in ("intl", "cn"):
            selected = account("same-user", realm)
            payload = {"storage_json": request(selected)["StorageJSON"], "metadata": {}, "host": {"proxy_url": ""}}
            with patch.object(selected, "fetch_credits", return_value={"ok": True, "credits": {
                    "remain": 350, "used": 50, "size": 400, "is_paid_user": False}}) as fetch:
                response = w.dispatch("quota.fetch", payload)
            fetch.assert_called_once_with()
            self.assertEqual(response["summary"][0]["value"], 350)
            self.assertEqual(response["subscription"]["plan"], "Free")
            self.assertEqual(response["groups"][0]["buckets"][0]["remainingFraction"], .875)

    def test_quota_errors_never_appear_as_zero_balance(self):
        selected = account()
        payload = {"storage_json": request(selected)["StorageJSON"]}
        for result in ({"ok": False, "error": "synthetic failure"}, {"ok": True, "credits": {}},
                       {"ok": True, "credits": {"remain": float("nan")}}, []):
            with patch.object(selected, "fetch_credits", return_value=result), self.assertRaises(w.BridgeError):
                w.dispatch("quota.fetch", payload)

    def test_disabled_quota_account_never_queries_upstream(self):
        selected = account()
        with patch.object(selected, "fetch_credits") as fetch, self.assertRaises(w.BridgeError):
            w.dispatch("quota.fetch", {"storage_json": request(selected)["StorageJSON"], "metadata": {"disabled": True}})
        fetch.assert_not_called()

    def test_models_separate_regions(self):
        for realm in ("intl", "cn"):
            a = account("u-" + realm, realm)
            with patch.object(w.HUB, "fetch_models", return_value=[("same-model", {"name": "Same"})]):
                result = w.dispatch("model.for_auth", request(a))
            self.assertEqual(result["Models"][0]["ID"], "workbuddy-" + realm + "/same-model")

    def test_nonstream_reuses_original_aggregator(self):
        a = account()
        with patch.object(w.HUB, "open_upstream", return_value=(stream(sample()), a, None)) as opener:
            result = w.execute(request(a))
        payload = json.loads(base64.b64decode(result["Payload"]))
        self.assertEqual(payload["choices"][0]["message"]["content"], "hello error world")
        self.assertEqual(payload["usage"]["total_tokens"], 7)
        self.assertIs(opener.call_args.kwargs["pin_account"], a)
        self.assertEqual(opener.call_args.kwargs["allowed_accounts"], {a.uid})
        self.assertFalse(opener.call_args.kwargs["allow_credit_fallback"])
        self.assertFalse(opener.call_args.kwargs["allow_product_switch"])

    def test_stream_tool_calls_usage_tail_done(self):
        a = account()
        output = []
        with patch.object(w.HUB, "open_upstream", return_value=(stream(sample(tool=True)), a, None)):
            w.execute(request(a), output.append)
        chunks = [json.loads(line[6:]) for line in b"".join(output).splitlines()
                  if line.startswith(b"data: ") and line != b"data: [DONE]"]
        self.assertTrue(any(c.get("choices", [{}])[0].get("delta", {}).get("tool_calls") for c in chunks if c.get("choices")))
        self.assertEqual(chunks[-1]["choices"], [])
        self.assertEqual(chunks[-1]["usage"]["prompt_tokens_details"]["cached_tokens"], 2)
        self.assertTrue(b"".join(output).endswith(b"data: [DONE]\n\n"))

    def test_truncated_stream_is_error_and_never_replayed(self):
        a = account()
        output = []
        with patch.object(w.HUB, "open_upstream", return_value=(stream(sample()[:1], False), a, None)) as opener:
            with self.assertRaises(w.BridgeError):
                w.execute(request(a), output.append)
            self.assertEqual(opener.call_count, 1)
        self.assertNotIn(b"data: [DONE]", b"".join(output))

    def test_truncated_nonstream_is_error(self):
        a = account()
        with patch.object(w.HUB, "open_upstream", return_value=(stream(sample()[:1], False), a, None)):
            with self.assertRaises(w.BridgeError):
                w.execute(request(a))

    def test_error_messages_do_not_leak_secret(self):
        self.assertNotIn("sensitive-secret", w.safe_error(RuntimeError("sensitive-secret"))["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
