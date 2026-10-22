"""真实 DLL + 隔离内核 + 模拟上游，端到端验证。"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid
import psutil
from native_client import NativeClient

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / ".work" / ("integration-" + uuid.uuid4().hex)
LIBDIR = RUN / "plugins/windows/amd64"
LIBDIR.mkdir(parents=True)
library = LIBDIR / "workbuddy-hub-v0.1.2.dll"
shutil.copy2(ROOT / "dist/workbuddy-hub.dll", library)
checks = []
def check(name, condition):
    if not condition:
        raise AssertionError(name)
    checks.append(name)
    print("PASS", name, flush=True)

# 用独立 DLL 解释器解包，测试与发布物内容一致，随后只在隔离副本添加模拟上游。
client = NativeClient(library)
check("native registration", client.call("plugin.register")["metadata"]["Name"] == "WorkBuddy")
start = client.call("auth.login.start", {"BaseURL": "http://127.0.0.1:18317"})
check("native OAuth start", start["Provider"] == "workbuddy-hub")
client.close()
runtime = LIBDIR / "workbuddy-hub/runtime-0.1.2"
worker = runtime / "worker.py"
mock = (ROOT / "tests/mock_runtime.py").read_text(encoding="utf-8")
injection = mock + """
_original_bootstrap = bootstrap
def bootstrap(data):
    value = _original_bootstrap(data)
    import sys
    install(sys.modules[__name__], data)
    return value
"""
source = worker.read_text(encoding="utf-8")
source = source.replace('if __name__ == "__main__":', injection + '\nif __name__ == "__main__":')
worker.write_text(source, encoding="utf-8")
shutil.copytree(runtime, RUN / "plugins/workbuddy-hub/runtime-0.1.2")
shutil.copy2(ROOT / ".work/core/cli-proxy-api.exe", RUN / "cli-proxy-api.exe")

old = ROOT / ".work/legacy/workbuddy.dll"
if old.exists():
    shutil.copy2(old, LIBDIR / "workbuddy-v0.9.8.dll")

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
base = "http://127.0.0.1:" + str(port)
config = RUN / "config.yaml"
config.write_text("host: 127.0.0.1\nport: " + str(port) +
    "\nremote-management:\n  secret-key: synthetic-management-key\n  allow-remote: false\n  disable-control-panel: true\n" +
    "api-keys: [synthetic-client-key]\nauth-dir: " + json.dumps(str(RUN / "auth")) +
    "\nrequest-retry: 0\nmax-retry-credentials: 1\nplugins:\n  enabled: true\n  dir: " + json.dumps(str(RUN / "plugins")) +
    "\n  configs:\n    workbuddy-hub:\n      enabled: true\n    workbuddy:\n      enabled: true\n      daily_bonus: false\n      checkin_auto: false\n      lifecycle_auto: false\n      token_keepalive: false\n", encoding="utf-8")
log = (RUN / "core.log").open("w", encoding="utf-8")
proc = None
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
no_redirect = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
def call(path, method="GET", payload=None, public=False):
    key = "synthetic-client-key" if public else "synthetic-management-key"
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode() if payload is not None else None,
        method=method, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with opener.open(req, timeout=35) as response:
        data = response.read()
        return json.loads(data) if "application/json" in response.headers.get("content-type", "") else data

def start_core():
    global proc, log
    proc = subprocess.Popen([str(RUN / "cli-proxy-api.exe"), "--config", str(config), "--local-model"],
        cwd=RUN, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW,
        env=dict(os.environ, TEMP=str(RUN), TMP=str(RUN)))
    for _ in range(80):
        if proc.poll() is not None:
            raise RuntimeError("core exited")
        try:
            return call("/v8/management/plugins")
        except Exception:
            time.sleep(.25)
    raise RuntimeError("core not ready")

def stop_core():
    children = psutil.Process(proc.pid).children(recursive=True) if proc.poll() is None else []
    proc.terminate()
    try:
        proc.wait(8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    gone, alive = psutil.wait_procs(children, timeout=8)
    check("backend exits with core", not alive)

def login(realm):
    start = call("/v8/management/oauth/auth-url?provider=workbuddy-hub")
    check(realm + " local OAuth URL", start["url"].startswith(base + "/v0/resource/plugins/workbuddy-hub/login"))
    with opener.open(start["url"], timeout=20) as page:
        text = page.read().decode()
        check(realm + " selection page", "国际版" in text and "国内版" in text)
    try:
        no_redirect.open(start["url"] + "&realm=" + realm, timeout=20)
        raise AssertionError("expected redirect")
    except urllib.error.HTTPError as e:
        print("REDIRECT",e.code,e.headers["Location"],flush=True)
        check(realm + " official redirect", e.code == 303 and e.headers["Location"] == "https://auth.example.test/" + realm)
    for _ in range(40):
        status = call("/v8/management/oauth/status?state=" + start["state"])
        if status.get("status") != "wait":
            break
        time.sleep(.25)
    check(realm + " OAuth persisted", status.get("status") == "ok")
    return start

try:
    plugins = start_core()
    entries = {x["id"]: x for x in plugins["plugins"]}
    check("core OAuth capability", entries["workbuddy-hub"]["supports_oauth"] and entries["workbuddy-hub"]["effective_enabled"])
    if old.exists():
        check("old CodeBuddy coexists", entries["workbuddy"]["effective_enabled"] and entries["workbuddy"]["supports_oauth"])
    login("intl")
    login("cn")
    for _ in range(40):
        models = call("/v1/models", public=True)
        ids = [x["id"] for x in models["data"]]
        if "workbuddy-intl/test-model" in ids and "workbuddy-cn/test-model" in ids:
            break
        time.sleep(.25)
    check("both region models registered", "workbuddy-intl/test-model" in ids and "workbuddy-cn/test-model" in ids)
    for realm in ("intl", "cn"):
        payload = {"model": "workbuddy-" + realm + "/test-model", "messages": [{"role": "user", "content": "hello"}], "stream": False}
        result = call("/v1/chat/completions", "POST", payload, public=True)
        check(realm + " chat nonstream", result["choices"][0]["message"]["content"] == "synthetic reply")
        check(realm + " usage preserved", result["usage"]["total_tokens"] == 7)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        result = call("/v1/chat/completions", "POST", payload, public=True)
        check(realm + " SSE stream", b"synthetic reply" in result and b"[DONE]" in result and b"cached_tokens" in result)
    response = call("/v1/responses", "POST", {"model": "workbuddy-intl/test-model", "input": "hello", "stream": False}, public=True)
    check("Responses conversion", response["object"] == "response" and "synthetic reply" in json.dumps(response))
    response = call("/v1/responses", "POST", {"model": "workbuddy-intl/test-model", "input": "hello", "stream": True}, public=True)
    check("Responses SSE conversion", b"response.completed" in response and b"synthetic reply" in response)
    slow_payload = {"model": "workbuddy-intl/test-model", "messages": [{"role": "user", "content": "slowstream"}],
        "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}], "stream": True}
    slow_req = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(slow_payload).encode(),
        headers={"Authorization": "Bearer synthetic-client-key", "Content-Type": "application/json"})
    began = time.monotonic()
    with opener.open(slow_req, timeout=20) as resp:
        first_line = resp.readline()
        first_time = time.monotonic() - began
        tail = resp.read()
    check("stream emits before upstream finishes", first_line.startswith(b"data: ") and first_time < 1.1 and time.monotonic() - began >= 1.3 and b"[DONE]" in tail)
    try:
        call("/v1/chat/completions", "POST", {"model": "workbuddy-intl/test-model",
            "messages": [{"role": "user", "content": "status429"}]}, public=True)
        raise AssertionError("expected 429")
    except urllib.error.HTTPError as e:
        check("upstream 429 status preserved", e.code == 429)
    time.sleep(1.5)
    result = call("/v1/chat/completions", "POST", {"model": "workbuddy-intl/test-model", "messages": [{"role": "user", "content": "hello"}],
        "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}], "stream": False}, public=True)
    check("tool call through native core", result["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "lookup")
    try:
        result = call("/v1/chat/completions", "POST", {"model": "workbuddy-intl/test-model",
            "messages": [{"role": "user", "content": "truncate"}], "stream": True}, public=True)
        check("truncated stream reports error", b"error" in result)
    except urllib.error.HTTPError as e:
        check("truncated stream reports error", e.code == 502 and b"error" in e.read())
    children = psutil.Process(proc.pid).children(recursive=True)
    python_children = [p for p in children if p.name().lower() == "python.exe"]
    check("one bundled Python backend", len(python_children) == 1)
    check("backend has no listening sockets", not [c for p in python_children for c in p.net_connections() if c.status == psutil.CONN_LISTEN])
    check("bundled runtime location", all(str(RUN).lower() in p.exe().lower() for p in python_children))
    ref_files = [p for p in (RUN / "auth").glob("*.json") if "workbuddy-hub" in p.name]
    check("host credentials contain no tokens", len(ref_files) == 2 and all("accessToken" not in p.read_text() and "refreshToken" not in p.read_text() for p in ref_files))
    credentials = call("/v8/management/credentials")["files"]
    check("core quota capability", entries["workbuddy-hub"].get("supports_quota") is True)
    for realm in ("intl", "cn"):
        credential = next(item for item in credentials if item.get("type") == "workbuddy-hub" and realm in item["name"])
        check(realm + " credential quota route", credential.get("supports_quota") is True)
        quota = call("/v8/management/plugins/workbuddy-hub/quota", "POST", {"auth_index": credential["auth_index"]})
        check(realm + " quota through native core", quota["summary"][0]["value"] == 350 and quota["subscription"]["plan"] == "Free")
    intl_file = next(x["name"] for x in credentials if x.get("type") == "workbuddy-hub" and "intl" in x["name"])
    call("/v8/management/credentials/status", "PATCH", {"name": intl_file, "disabled": True})
    time.sleep(.3)
    try:
        call("/v1/chat/completions", "POST", {"model": "workbuddy-intl/test-model", "messages": [{"role": "user", "content": "disabled"}]}, public=True)
        raise AssertionError("disabled credential executed")
    except urllib.error.HTTPError as e:
        check("host disabled credential cannot execute", e.code in (400, 401, 403, 404, 429, 503))
    call("/v8/management/credentials/status", "PATCH", {"name": intl_file, "disabled": False})
    stop_core()
    start_core()
    for _ in range(60):
        if any(x["id"] == "workbuddy-cn/test-model" for x in call("/v1/models", public=True)["data"]):
            break
        time.sleep(.25)
    result = call("/v1/chat/completions", "POST", {"model": "workbuddy-cn/test-model", "messages": [{"role": "user", "content": "restart"}]}, public=True)
    check("restart restores account references", result["choices"][0]["message"]["content"] == "synthetic reply")
    backend_before_disable = [p for p in psutil.Process(proc.pid).children(recursive=True) if p.name().lower() == "python.exe"]
    call("/v8/management/config/plugins/configs/workbuddy-hub/enabled", "PUT", False)
    for _ in range(40):
        if not any(p.is_running() for p in backend_before_disable):
            break
        time.sleep(.2)
    check("disabling plugin stops backend", not any(p.is_running() for p in backend_before_disable))
    stop_core()
    report = {"checks_passed": len(checks), "library_sha256": hashlib.sha256(library.read_bytes()).hexdigest(), "checks": checks, "upstream_mode": "simulated",
              "core": "8.0.38", "core_sha256": hashlib.sha256((RUN / "cli-proxy-api.exe").read_bytes()).hexdigest(), "gui_source": "0.3.50", "plugin_entries": list(entries.values()), "real_account_authorization": "not_run",
              "real_model_generation": "not_run", "existing_install_modified": False, "run_directory": str(RUN)}
    (ROOT / "dist/integration-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("REPORT", json.dumps(report, ensure_ascii=False), flush=True)
except Exception:
    log.flush()
    print("CORE_LOG", (RUN / "core.log").read_text(encoding="utf-8", errors="replace")[-8500:], flush=True)
    raise
finally:
    if proc and proc.poll() is None:
        stop_core()
    log.close()
