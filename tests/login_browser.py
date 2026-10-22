"""真实 Chromium 与 CSP 下验证区域跳转，仅使用虚构的授权站点。"""
import base64
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import ipaddress
from pathlib import Path
import sys
import ssl
import threading
import urllib.parse
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / ".work" / ("login-browser-" + uuid.uuid4().hex)
RUN.mkdir(parents=True)
os.environ.update(TEMP=str(RUN), TMP=str(RUN))
sys.path[:0] = [str(ROOT), str(ROOT / ".work/qa-python")]
from playwright.sync_api import sync_playwright
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import worker

worker.bootstrap(RUN / "data")


class ResourceHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        request = {"Method": "GET", "Query": urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query),
                   "Headers": {name: [value] for name, value in self.headers.items()}}
        try:
            response = worker.resource(request)
        except worker.BridgeError as error:
            response = worker.http_response(error.status, str(error).encode())
        self.send_response(response["StatusCode"])
        for name, values in response["Headers"].items():
            for value in values:
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(base64.b64decode(response["Body"]))

    def log_message(self, *args):
        pass


class OfficialHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<h1>Synthetic official authorization</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
now = datetime.datetime.now(datetime.timezone.utc)
certificate = x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(
    x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1)).not_valid_after(
    now + datetime.timedelta(hours=1)).add_extension(x509.SubjectAlternativeName([
        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False).sign(key, hashes.SHA256())
(RUN / "mock-key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
(RUN / "mock-cert.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
official = ThreadingHTTPServer(("127.0.0.1", 0), OfficialHandler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(RUN / "mock-cert.pem", RUN / "mock-key.pem")
official.socket = context.wrap_socket(official.socket, server_side=True)
official_thread = threading.Thread(target=official.serve_forever, daemon=True)
official_thread.start()
official_origin = "https://127.0.0.1:" + str(official.server_port)
server = ThreadingHTTPServer(("127.0.0.1", 0), ResourceHandler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
origin = "http://127.0.0.1:" + str(server.server_port)
checks = []
try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch_persistent_context(str(RUN / "profile"), channel="msedge",
            headless=True, ignore_https_errors=True, args=["--no-proxy-server", "--disable-crash-reporter"])
        try:
            for realm, label in [("intl", "国际版 · WorkBuddy"), ("cn", "国内版 · CodeBuddy / WorkBuddy")]:
                with patch.object(worker.POOLS[realm], "start_login", return_value={
                        "state": "synthetic-" + realm, "authUrl": official_origin + "/" + realm}) as start:
                    session = worker.login_start({"BaseURL": origin})
                    page = browser.new_page()
                    violations = []
                    page.on("console", lambda message: violations.append(message.text)
                            if "Content Security Policy" in message.text else None)
                    page.goto(session["URL"])
                    page.get_by_text(label, exact=True).click()
                    try:
                        page.wait_for_url(official_origin + "/" + realm, timeout=5000)
                    except Exception:
                        print(json.dumps({"realm": realm, "url": page.url, "csp_violations": violations}), flush=True)
                        raise
                    assert page.get_by_role("heading", name="Synthetic official authorization").is_visible()
                    assert not violations, violations
                    checks.append(realm + " browser follows official redirect with CSP enabled")
                    retry = browser.request.get(session["URL"] + "&realm=" + realm, max_redirects=0)
                    assert retry.status == 303, retry.text()
                    assert retry.headers["location"] == official_origin + "/" + realm
                    assert start.call_count == 1
                    checks.append(realm + " retry reuses the same upstream authorization")
                    page.close()
        finally:
            browser.close()
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    official.shutdown()
    official.server_close()
    official_thread.join(timeout=5)

report = {"checks": checks, "passed": True, "real_authorization": False,
          "browser": "Microsoft Edge Chromium", "csp_enforced": True}
(ROOT / "dist/browser-login-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, ensure_ascii=False), flush=True)
