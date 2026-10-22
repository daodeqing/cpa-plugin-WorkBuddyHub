"""WorkBuddy2API-Hub v1.7.24 -> EasyCPA。只运行 stdio，不监听 HTTP。"""
import base64
import concurrent.futures
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.error

PROVIDER = "workbuddy-hub"
VERSION = "0.1.0"
ROOT = Path(__file__).resolve().parent
WIRE = sys.stdout
WIRE_LOCK = threading.Lock()
sys.stdout = sys.stderr
DATA = None
HUB = None
POOLS = {}
SESSIONS = {}
SESSION_LOCK = threading.RLock()
LOCAL = threading.local()
TTL = 300
ACTIVE = {}
ACTIVE_LOCK = threading.Lock()


class BridgeError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def b64(data):
    return base64.b64encode(data).decode("ascii")


def unbytes(value):
    return base64.b64decode(value, validate=True) if isinstance(value, str) else bytes(value or [])


def wire(value):
    with WIRE_LOCK:
        WIRE.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        WIRE.flush()


class MultiPool:
    """每区域保留原版账号池；不会把同 UID 的两区域账号混在一起。"""
    @property
    def accounts(self):
        return [a for p in POOLS.values() for a in p.accounts]

    def __getattr__(self, name):
        if name.startswith("apply_") or name == "restore_model_caps":
            return lambda *a, **kw: [getattr(p, name)(*a, **kw) for p in POOLS.values()]
        raise AttributeError(name)

    def count_ready(self, realm=None, **kw):
        return sum(p.count_ready(realm=realm, **kw) for p in POOLS.values())

    def get(self, uid):
        return POOLS[getattr(LOCAL, "realm", "intl")].get(uid)

    def representative(self, realm=None):
        return POOLS[realm or getattr(LOCAL, "realm", "intl")].representative(realm=realm)

    def pick_for_session(self, realm=None, **kw):
        return POOLS[realm or getattr(LOCAL, "realm", "intl")].pick_for_session(realm=realm, **kw)


def bootstrap(data):
    global DATA, HUB
    DATA = Path(data).resolve()
    for name in ("accounts", "usage", "catalog", "tmp"):
        (DATA / name).mkdir(parents=True, exist_ok=True)
    os.environ.update(ACCOUNTS_DIR=str(DATA / "accounts"), WB_PROXY_USAGE_DIR=str(DATA / "usage"),
                      WB_PROXY_CATALOG_DIR=str(DATA / "catalog"), TMP=str(DATA / "tmp"), TEMP=str(DATA / "tmp"),
                      PYTHONDONTWRITEBYTECODE="1", WB_BACKGROUND_MODEL_NAMES="")
    os.chdir(DATA)
    sys.path.insert(0, str(ROOT / "upstream"))
    import wb_proxy
    HUB = wb_proxy
    HUB.log = lambda *a, **kw: None
    # poll_login 原版会给国内账号做首次签到；本版仅登录，不执行签到或成长。
    HUB.wb_accounts.Account.checkin = lambda *a, **kw: {"adapter_disabled": True}
    for realm in ("intl", "cn"):
        pool = HUB.wb_accounts.AccountPool(str(DATA / "accounts" / realm), log=lambda *a: None)
        pool.load()
        POOLS[realm] = pool
    HUB.POOL = MultiPool()
    HUB.SYSTEM_PROMPT = HUB.DEFAULT_SYSTEM_PROMPT
    original_json = HUB.wb_accounts.http_json
    def scoped_json(*a, **kw):
        if not kw.get("proxy"):
            kw["proxy"] = getattr(LOCAL, "proxy", "")
        return original_json(*a, **kw)
    HUB.wb_accounts.http_json = scoped_json
    original_open = HUB.urllib.request.urlopen
    def scoped_open(*a, **kw):
        proxy = getattr(LOCAL, "proxy", "")
        if proxy:
            return HUB.wb_accounts.opener_for_proxy(proxy).open(*a, **kw)
        return original_open(*a, **kw)
    HUB.urllib.request.urlopen = scoped_open
    return {"version": VERSION, "upstream": "1.7.24", "pid": os.getpid(), "listener": False}


def reference(account):
    return {"type": PROVIDER, "schema": 1, "uid": account.uid, "realm": account.realm,
            "nickname": account.nickname or ""}


def parse_ref(value):
    try:
        ref = json.loads(unbytes(value))
    except Exception:
        raise BridgeError("账号引用格式无效", 400)
    if ref.get("type") != PROVIDER or ref.get("realm") not in ("intl", "cn") or not isinstance(ref.get("uid"), str) or not ref["uid"]:
        raise BridgeError("账号不属于 WorkBuddy Hub", 400)
    return ref


def auth_data(ref, previous=None, filename=None):
    metadata = dict(previous or {})
    metadata.update(type=PROVIDER, realm=ref["realm"], uid=ref["uid"], nickname=ref.get("nickname", ""))
    disabled = bool(metadata.get("disabled", False))
    storage = dict(ref, disabled=disabled)
    digest = hashlib.sha256((ref["realm"] + ":" + ref["uid"]).encode()).hexdigest()[:24]
    return {"Provider": PROVIDER, "ID": "", "FileName": filename if filename is not None else
            PROVIDER + "-" + ref["realm"] + "-" + digest + ".json",
            "Label": "WorkBuddy " + ref["realm"] + " · " + (ref.get("nickname") or ref["uid"][:8]),
            "Disabled": disabled, "StorageJSON": b64(json.dumps(storage).encode()),
            "Metadata": metadata, "Attributes": {"realm": ref["realm"]}}


def account_for(req):
    ref = parse_ref(req.get("StorageJSON"))
    meta = req.get("AuthMetadata") or req.get("Metadata") or {}
    if ref.get("disabled") or meta.get("disabled"):
        raise BridgeError("所选账号已停用", 403)
    account = POOLS[ref["realm"]].get(ref["uid"])
    if not account or not account.enabled:
        raise BridgeError("账号凭据不存在或已停用，请重新登录", 401)
    LOCAL.realm = ref["realm"]
    LOCAL.proxy = ((req.get("Host") or {}).get("ProxyURL") or account.proxy_legacy or "")
    account.proxy = LOCAL.proxy
    return ref, account


def sweep_sessions():
    now = time.time()
    with SESSION_LOCK:
        for state, s in list(SESSIONS.items()):
            if s["expires"] <= now:
                if s.get("upstream_state"):
                    POOLS[s["realm"]].cancel_login(s["upstream_state"])
                del SESSIONS[state]


def session_for(ticket):
    sweep_sessions()
    with SESSION_LOCK:
        if ticket not in SESSIONS:
            raise BridgeError("登录已取消或过期，请从 EasyCPA 重新发起登录", 410)
        return SESSIONS[ticket]


def login_start(req):
    sweep_sessions()
    base = urllib.parse.urlsplit(req.get("BaseURL") or "")
    if base.scheme not in ("http", "https") or not base.netloc or base.username or base.password:
        raise BridgeError("EasyCPA 没有提供有效的 API 地址", 400)
    origin = urllib.parse.urlunsplit((base.scheme, base.netloc, "", "", ""))
    state = secrets.token_urlsafe(32)
    host = req.get("Host") or {}
    with SESSION_LOCK:
        if len(SESSIONS) >= 128:
            raise BridgeError("登录会话过多，请稍后重试", 429)
        SESSIONS[state] = {"expires": time.time() + TTL, "phase": "select", "origin": origin,
                           "realm": None, "upstream_state": None, "proxy": host.get("ProxyURL") or ""}
    return {"Provider": PROVIDER, "URL": origin + "/v0/resource/plugins/" + PROVIDER + "/login?ticket=" + state,
            "State": state, "ExpiresAt": datetime.datetime.fromtimestamp(time.time() + TTL, datetime.timezone.utc).isoformat(),
            "Metadata": {"flow": "poll"}}


def choose(ticket, realm):
    if realm not in POOLS:
        raise BridgeError("请选择国内版或国际版", 400)
    s = session_for(ticket)
    with SESSION_LOCK:
        if s["realm"] == realm and s["phase"] in ("waiting", "polling") and s.get("auth_url"):
            return s["auth_url"]
        if s["phase"] != "select":
            raise BridgeError("区域已经选择，请返回 EasyCPA 查看授权状态", 409)
        s.update(phase="starting", realm=realm)
    try:
        # 代理按线程绑定，不改变其他账号或登录会话。
        LOCAL.proxy = s["proxy"]
        started = POOLS[realm].start_login(realm=realm)
        parsed = urllib.parse.urlsplit(started["authUrl"])
        if parsed.scheme != "https" or not parsed.hostname or parsed.username:
            raise BridgeError("上游授权地址无效")
        with SESSION_LOCK:
            if ticket not in SESSIONS:
                POOLS[realm].cancel_login(started["state"])
                raise BridgeError("登录已经取消", 410)
            s.update(phase="waiting", upstream_state=started["state"], auth_url=started["authUrl"])
        return started["authUrl"]
    except Exception:
        with SESSION_LOCK:
            if ticket in SESSIONS:
                s["phase"] = "error"
        raise


def login_poll(req):
    state = req.get("State", "")
    s = session_for(state)
    with SESSION_LOCK:
        if s["phase"] == "success":
            return {"Status": "success", "Auth": s["auth"]}
        if s["phase"] == "error":
            return {"Status": "error", "Message": "授权失败，请重新发起登录"}
        if s["phase"] != "waiting":
            return {"Status": "pending"}
        s["phase"] = "polling"
    try:
        LOCAL.proxy = s["proxy"]
        result = POOLS[s["realm"]].poll_login(s["upstream_state"])
        with SESSION_LOCK:
            if state not in SESSIONS:
                return {"Status": "error", "Message": "登录已经取消"}
            if result["status"] == "ok":
                account = POOLS[s["realm"]].get(result["account"]["uid"])
                account.tasks_enabled = False
                account.proxy = account.proxy_legacy = s["proxy"]
                account.save(POOLS[s["realm"]].dir)
                s.update(phase="success", auth=auth_data(reference(account)))
                return {"Status": "success", "Auth": s["auth"]}
            s["phase"] = "waiting" if result["status"] == "pending" else "error"
            return {"Status": "pending"} if s["phase"] == "waiting" else {"Status": "error", "Message": "授权过期或失败"}
    except Exception:
        with SESSION_LOCK:
            s["phase"] = "error"
        raise


def cancel(req):
    with SESSION_LOCK:
        s = SESSIONS.pop(req.get("State", ""), None)
    if s and s.get("upstream_state"):
        POOLS[s["realm"]].cancel_login(s["upstream_state"])
    return {}


def http_response(status, body, extra=None):
    h = {"content-type": ["text/html; charset=utf-8"], "cache-control": ["no-store"],
         "referrer-policy": ["no-referrer"], "x-content-type-options": ["nosniff"],
         "content-security-policy": ["default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"]}
    h.update(extra or {})
    return {"StatusCode": status, "Headers": h, "Body": b64(body)}


def resource(req):
    ticket = ((req.get("Query") or {}).get("ticket") or [""])[0]
    s = session_for(ticket)
    headers = {k.lower(): v for k, v in (req.get("Headers") or {}).items()}
    origin = headers.get("origin", [""])
    origin = origin[0] if isinstance(origin, list) else origin
    site = headers.get("sec-fetch-site", [""])
    site = site[0] if isinstance(site, list) else site
    if (origin and origin != s["origin"]) or site in ("cross-site",):
        raise BridgeError("跨站登录请求已拒绝", 403)
    realm = ((req.get("Query") or {}).get("realm") or [""])[0]
    if req.get("Method", "GET") == "GET" and realm:
        url = choose(ticket, realm)
        return http_response(303, b"", {"location": [url]})
    if req.get("Method", "GET") != "GET":
        raise BridgeError("不支持的请求方式", 405)
    if s["phase"] != "select":
        return http_response(200, "授权已经发起，请在官方页面完成登录后返回 EasyCPA。".encode())
    return http_response(200, (ROOT / "login.html").read_text(encoding="utf-8").replace("TICKET_PLACEHOLDER", ticket).encode())


class TrackedResponse:
    """把无终止帧的 EOF 变为上游异常，不允许截断流伪装完成。"""
    def __init__(self, response):
        self.response = response
        self.tail = b""
        self.terminal = False

    def __getattr__(self, name):
        return getattr(self.response, name)

    def read1(self, size):
        chunk = self.response.read1(size) if hasattr(self.response, "read1") else self.response.readline(size)
        if chunk:
            self.tail += chunk
            lines = self.tail.split(b"\n")
            self.tail = lines.pop()
            for line in lines:
                if line.strip() == b"data: [DONE]":
                    self.terminal = True
                try:
                    obj = json.loads(line.removeprefix(b"data:").strip())
                    if any(c.get("finish_reason") for c in obj.get("choices", [])):
                        self.terminal = True
                except Exception:
                    pass
        elif not self.terminal:
            raise BridgeError("上游连接提前中断，未收到结束标志")
        return chunk


class Sink:
    def __init__(self, emit):
        self.emit = emit
        self.error = False
        self.done = False

    def write(self, data):
        # 必须按 SSE 帧解析错误，不能把模型输出中的 error 字样当成失败。
        for line in data.splitlines():
            if line.startswith(b"data: "):
                try:
                    obj = json.loads(line[6:])
                    if isinstance(obj, dict) and "error" in obj:
                        self.error = True
                except Exception:
                    pass
        self.done |= b"data: [DONE]" in data
        self.emit(data)
        return len(data)

    def flush(self):
        pass


def execute(req, emit=None):
    ref, account = account_for(req)
    payload = json.loads(unbytes(req.get("Payload") or req.get("OriginalRequest")))
    requested = req.get("Model") or payload.get("model") or ""
    prefix = "workbuddy-" + ref["realm"] + "/"
    if not requested.startswith(prefix) or len(requested) == len(prefix):
        raise BridgeError("模型区域与所选账号不匹配", 400)
    model = requested[len(prefix):]
    payload["model"] = model
    payload["stream"] = bool(emit)
    started = time.time()
    response, chosen, effort = HUB.open_upstream(payload, target_realm=ref["realm"],
        pin_account=account, allowed_accounts={account.uid}, allow_credit_fallback=False,
        allow_product_switch=False)
    if chosen.uid != account.uid or chosen.realm != ref["realm"]:
        response.close()
        raise BridgeError("上游账号选择不一致，已终止请求")
    cancel_event = getattr(LOCAL, "cancel", threading.Event())
    with ACTIVE_LOCK:
        if hasattr(LOCAL, "rpc_id"):
            ACTIVE[LOCAL.rpc_id] = (cancel_event, response)
    handler = object.__new__(HUB.Handler)
    handler.path = "/v1/chat/completions"
    handler.headers = {}
    handler._key_id = lambda: "easycpa"
    handler.send_response = lambda *a: None
    handler.send_header = lambda *a: None
    handler.end_headers = lambda: None
    captured = {}
    handler._json = lambda status, value: captured.update(status=status, value=value)
    def fail(status, message, *a, **kw):
        raise BridgeError(str(message), status)
    handler._error = fail
    def output(data):
        if cancel_event.is_set():
            raise BrokenPipeError("client cancelled")
        emit(data)
    sink = Sink(output if emit else lambda data: None)
    handler.wfile = sink
    try:
        tracked = TrackedResponse(response)
        if emit:
            HUB.Handler._chat_stream_response(handler, tracked, requested, None, chosen, started,
                                               effort=effort, reopen=None)
            if sink.error or not sink.done:
                raise BridgeError("WorkBuddy 上游流未正常完成")
            return {"Headers": {"content-type": ["text/event-stream"]}}
        HUB.Handler._chat_nonstream_response(handler, tracked, model, None, chosen, started, effort=effort)
        result = captured["value"]
        result["model"] = requested
        return {"Payload": b64(json.dumps(result, ensure_ascii=False).encode()),
                "Headers": {"content-type": ["application/json"]}}
    finally:
        response.close()
        with ACTIVE_LOCK:
            if hasattr(LOCAL, "rpc_id"):
                ACTIVE.pop(LOCAL.rpc_id, None)


def quota_fetch(req):
    host = req.get("host") or {}
    ref, account = account_for({"StorageJSON": req.get("storage_json"), "Metadata": req.get("metadata") or {},
        "Host": {"ProxyURL": host.get("proxy_url") or host.get("ProxyURL") or ""}})
    result = account.fetch_credits()
    if not isinstance(result, dict) or not result.get("ok"):
        message = result.get("error") if isinstance(result, dict) else None
        raise BridgeError(message or "上游额度查询失败", 401 if isinstance(result, dict) and result.get("need_relogin") else 502)
    credits = result.get("credits")
    if not isinstance(credits, dict):
        raise BridgeError("上游没有返回额度数据")
    summary = []
    for key, label in [("remain", "剩余积分"), ("used", "已用积分"), ("size", "总积分")]:
        value = credits.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            summary.append({"key": key, "label": label, "value": value, "unit": "积分", "format": "number"})
    if not any(metric["key"] == "remain" for metric in summary):
        raise BridgeError("上游没有返回有效的剩余积分")
    response = {"summary": summary, "groups": []}
    amounts = {metric["key"]: metric["value"] for metric in summary}
    if amounts.get("size", 0) > 0:
        response["groups"].append({"displayName": "WorkBuddy " + ref["realm"], "buckets": [{
            "window": "积分余额", "remainingFraction": max(0, min(1, amounts["remain"] / amounts["size"]))}]})
    if isinstance(credits.get("is_paid_user"), bool):
        response["subscription"] = {"plan": "Pro" if credits["is_paid_user"] else "Free"}
    return response


def dispatch(method, req, emit=None):
    if method == "auth.login.start":
        return login_start(req)
    if method == "auth.login.poll":
        return login_poll(req)
    if method == "auth.cancel":
        return cancel(req)
    if method == "management.handle":
        return resource(req)
    if method == "auth.parse":
        try:
            ref = parse_ref(req.get("RawJSON"))
        except BridgeError:
            return {"Handled": False}
        previous = json.loads(unbytes(req["RawJSON"]))
        return {"Handled": True, "Auth": auth_data(ref, previous, req.get("FileName"))}
    if method == "auth.refresh":
        ref, account = account_for(req)
        if account.refresh() is False:
            raise BridgeError("账号凭据刷新失败，请重新登录", 401)
        return {"Auth": auth_data(reference(account), req.get("Metadata"), "")}
    if method == "model.static":
        return {"Provider": PROVIDER, "Models": []}
    if method == "model.for_auth":
        ref, account = account_for(req)
        entries = HUB.fetch_models(ref["realm"])
        models = []
        for model, entry in entries:
            models.append({"ID": "workbuddy-" + ref["realm"] + "/" + model, "Object": "model",
                           "OwnedBy": PROVIDER, "DisplayName": entry.get("name") or model,
                           "ContextLength": entry.get("context_length", 0), "UserDefined": True,
                           "SupportedGenerationMethods": ["chat"]})
        return {"Provider": PROVIDER, "Models": models}
    if method == "quota.fetch":
        return quota_fetch(req)
    if method in ("executor.execute", "executor.execute_stream"):
        return execute(req, emit if method.endswith("_stream") else None)
    raise BridgeError("未实现的插件操作", 400)


def safe_error(exc):
    status = getattr(exc, "status", None) or getattr(exc, "code", None) or 502
    if HUB is not None and isinstance(exc, HUB.RateLimited):
        status = 429
    if HUB is not None and isinstance(exc, HUB.LocalNetworkError):
        status = 503
    status = status if isinstance(status, int) and 400 <= status <= 599 else 502
    if isinstance(exc, BridgeError):
        message = str(exc)
    elif isinstance(exc, urllib.error.HTTPError):
        message = "WorkBuddy 上游返回 HTTP " + str(status)
    else:
        message = "WorkBuddy 操作失败（" + type(exc).__name__ + "），请检查网络或重新登录"
    return {"code": "workbuddy_hub_error", "message": message, "http_status": status}


def serve():
    wire({"ready": bootstrap(Path(sys.argv[1]))})
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="wbh")
    def run(item):
        rpc_id = item["id"]
        LOCAL.rpc_id, LOCAL.cancel = rpc_id, threading.Event()
        try:
            result = dispatch(item["method"], item.get("request") or {},
                              emit=lambda data: wire({"id": rpc_id, "chunk": b64(data)}))
            wire({"id": rpc_id, "result": result})
        except Exception as exc:
            wire({"id": rpc_id, "error": safe_error(exc)})
    def sweep():
        while True:
            time.sleep(10)
            sweep_sessions()
    threading.Thread(target=sweep, daemon=True).start()
    try:
        for line in sys.stdin:
            item = json.loads(line)
            if item.get("method") == "shutdown":
                break
            if item.get("method") == "cancel_request":
                with ACTIVE_LOCK:
                    active = ACTIVE.get((item.get("request") or {}).get("id"))
                if active:
                    active[0].set()
                    try:
                        active[1].close()
                    except Exception:
                        pass
                continue
            executor.submit(run, item)
    finally:
        # stdin EOF 表示宿主已退出，不能让 ThreadPoolExecutor 的等待留下孤儿进程。
        os._exit(0)


if __name__ == "__main__":
    serve()
