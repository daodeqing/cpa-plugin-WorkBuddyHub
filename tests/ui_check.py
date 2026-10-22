"""用实际内核元数据渲染 EasyCPA 原有 OAuth 组件，不改主程序。"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
WORK = ROOT / ".work"
UI = WORK / "ui"
UI.mkdir(exist_ok=True)
os.environ.update(TEMP=str(WORK / "tmp"), TMP=str(WORK / "tmp"))
sys.path.insert(0, str(WORK / "qa-python"))
from playwright.sync_api import sync_playwright
entries = json.loads((ROOT / "dist/integration-report.json").read_text(encoding="utf-8"))["plugin_entries"]
(UI / "entries.json").write_text(json.dumps(entries), encoding="utf-8")
(UI / "index.html").write_text('<html lang="zh-CN"><head><meta charset="utf-8"></head><body><div id="root"></div><script type="module" src="./fixture.tsx"></script></body></html>', encoding="utf-8")
(UI / "fixture.tsx").write_text("""
import {createRoot} from 'react-dom/client';
import {mockIPC} from '@tauri-apps/api/mocks';
import {I18nProvider} from '../../../src/i18n';
import {PluginOAuthProviders} from '../../../src/pages/PluginOAuthProviders';
import '../../../src/styles/index.css';
import entries from './entries.json';
localStorage.setItem('easy-cli-proxy-api.locale','zh-CN');
document.documentElement.dataset.theme='dark';
(window as any).calls=[];
mockIPC(async(cmd,args:any)=>{
 (window as any).calls.push({cmd,args});
 if(cmd==='get_plugin_support')return true;
 if(cmd==='set_app_locale')return null;
 if(cmd==='management_request')return {plugins_enabled:true,plugins_dir:'plugins',plugins:entries};
 if(cmd==='start_oauth_login')return {url:'http://127.0.0.1:18317/v0/resource/plugins/workbuddy-hub/login?ticket=synthetic-ui-session',state:'synthetic-ui-session',expiresIn:300,opened:false};
 if(cmd==='get_oauth_status')return {status:'wait'};
 throw new Error('Unexpected '+cmd);
},{shouldMockEvents:true});
createRoot(document.getElementById('root')!).render(<I18nProvider><main style={{padding:24,maxWidth:1100,margin:'auto'}}><h1>OAuth</h1><div className="oauth-grid"><PluginOAuthProviders builtInProviderIds={[]} browser="none"/></div></main></I18nProvider>);
""", encoding="utf-8")
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
node = WORK / "qa-python/playwright/driver/node.exe"
script = UI / "vite.mjs"
script.write_text("""
import {createServer} from 'vite';
import react from '@vitejs/plugin-react';
const server=await createServer({configFile:false,root:process.argv[2],cacheDir:process.argv[3],
 plugins:[react()],logLevel:'error',server:{host:'127.0.0.1',port:Number(process.argv[4]),strictPort:true,watch:null}});
await server.listen();
console.log('READY');
""", encoding="utf-8")
env = dict(os.environ)
proc = subprocess.Popen([str(node), str(script), str(PROJECT), str(UI / "cache"), str(port)],
    cwd=PROJECT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
checks = []
base = "http://127.0.0.1:" + str(port)
dist = ROOT / "dist"
try:
    for _ in range(100):
        if proc.poll() is not None:
            raise RuntimeError(proc.stderr.read().decode())
        try:
            urllib.request.urlopen(base + "/cpa-plugin-WorkBuddyHub/.work/ui/index.html", timeout=1)
            break
        except Exception:
            time.sleep(.2)
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(str(UI / "browser-profile"), channel="msedge", headless=True,
            args=["--no-proxy-server", "--disable-crash-reporter"], viewport={"width": 1440, "height": 950})
        try:
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/cpa-plugin-WorkBuddyHub/.work/ui/index.html", wait_until="networkidle")
            page.get_by_role("heading", name="WorkBuddy OAuth", exact=True).wait_for()
            page.get_by_role("heading", name="CodeBuddy Intl CPA OAuth", exact=True).wait_for()
            assert page.locator(".oauth-card").count() == 2
            checks.append("WorkBuddy and old CodeBuddy OAuth cards render together")
            page.screenshot(path=str(dist / "oauth-cards.png"), full_page=True)
            card = page.locator(".oauth-card").filter(has=page.get_by_role("heading", name="WorkBuddy OAuth", exact=True))
            card.get_by_role("button", name="开始登录", exact=True).click()
            page.get_by_role("dialog").wait_for()
            page.wait_for_function("window.calls.some(x=>x.cmd==='start_oauth_login')")
            starts = page.evaluate("window.calls.filter(x=>x.cmd==='start_oauth_login')")
            assert starts[-1]["args"]["provider"] == "workbuddy-hub"
            checks.append("WorkBuddy login button sends independent provider ID")
            page.get_by_role("button", name="取消", exact=True).click()
            checks.append("OAuth dialog can be cancelled")
            # 登录选择页作为本地资源渲染，不请求任何真实官方授权地址。
            page.goto(base + "/cpa-plugin-WorkBuddyHub/login.html")
            page.get_by_role("link", name="国际版 · WorkBuddy", exact=True).wait_for()
            page.get_by_role("link", name="国内版 · CodeBuddy / WorkBuddy", exact=True).wait_for()
            checks.append("regional login buttons visible")
            page.screenshot(path=str(dist / "login-regions.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            checks.append("regional login has no mobile horizontal overflow")
            page.screenshot(path=str(dist / "login-regions-mobile.png"), full_page=True)
            assert not errors, errors
            checks.append("no browser JavaScript errors")
        finally:
            browser.close()
    report = {"checks_passed": len(checks), "checks": checks, "mode": "actual EasyCPA component with mock IPC and actual core metadata",
              "full_desktop_app_installed": False}
    (dist / "ui-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
finally:
    proc.terminate()
    try:
        proc.wait(8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
