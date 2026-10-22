"""下载隔离集成测试所需的已校验内核与旧插件；不会启动服务或读真实账号。"""
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ASSETS = [
    ("https://github.com/router-for-me/CLIProxyAPI/releases/download/v8.0.38/",
     "CLIProxyAPI_8.0.38_windows_amd64.zip", "core"),
    ("https://github.com/zidanefaqih/codebuddy-intl-cpa/releases/download/v0.9.8/",
     "workbuddy_0.9.8_windows_amd64.zip", "legacy"),
]


def main():
    for base, name, folder in ASSETS:
        checks = urllib.request.urlopen(base + "checksums.txt", timeout=30).read().decode()
        expected = next(x.split()[0] for x in checks.splitlines() if x.split()[-1] == name)
        raw = urllib.request.urlopen(base + name, timeout=90).read()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            raise RuntimeError("下载校验失败：" + name)
        target = ROOT / ".work" / folder
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for entry in z.infolist():
                if entry.is_dir():
                    continue
                relative = Path(entry.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise RuntimeError("压缩包路径越界")
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(z.read(entry))
        (target / "asset-proof.json").write_text(json.dumps(
            {"url": base + name, "sha256": actual, "bytes": len(raw)}, indent=2), encoding="utf-8")
        print(name, "verified", actual)


if __name__ == "__main__":
    main()
