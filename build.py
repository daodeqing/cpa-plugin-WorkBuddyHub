"""项目内构建 Windows amd64 插件，所有缓存均限制在本插件目录。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parent
VERSION = "0.1.2"


def verify_upstream():
    manifest = json.loads((ROOT / "upstream-manifest.json").read_text(encoding="utf-8"))
    for name, entry in manifest["files"].items():
        path = ROOT / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise RuntimeError("上游文件哈希不一致：" + name)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--zig", type=Path, default=ROOT / ".tools/zig/zig.exe")
    args = parser.parse_args()
    manifest = verify_upstream()
    work = ROOT / ".work"
    dist = ROOT / "dist"
    for p in (work, dist, work / "tmp"):
        p.mkdir(parents=True, exist_ok=True)
    payload = work / "payload.zip"
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_STORED) as z:
        for name in ("worker.py", "login.html", "upstream-manifest.json"):
            z.write(ROOT / name, name)
        for name in sorted(manifest["files"]):
            z.write(ROOT / name, name)
    assembly = work / "payload.s"
    assembly.write_text('# payload sha256 ' + hashlib.sha256(payload.read_bytes()).hexdigest() + '\n.section .rdata,"dr"\n.global wbh_payload_start\nwbh_payload_start:\n.incbin "' +
                        payload.as_posix() + '"\n.global wbh_payload_end\nwbh_payload_end:\n', encoding="utf-8")
    env = dict(os.environ, ZIG_LOCAL_CACHE_DIR=str(work / "zig-local"),
               ZIG_GLOBAL_CACHE_DIR=str(work / "zig-global"), TMP=str(work / "tmp"), TEMP=str(work / "tmp"))
    library = dist / "workbuddy-hub.dll"
    command = [str(args.zig.resolve()), "cc", "-target", "x86_64-windows-gnu", "-O2",
               "-shared", "-Wall", "-Wextra", "-Werror=implicit-function-declaration",
               "-I", str(ROOT / "vendor/cjson"), str(ROOT / "native/plugin.c"),
               str(ROOT / "vendor/cjson/cJSON.c"), str(assembly),
               "-o", str(library)]
    subprocess.run(command, cwd=ROOT, env=env, check=True)
    archive = dist / ("workbuddy-hub_" + VERSION + "_windows_amd64.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.write(library, library.name)
        for name in ("README.md", "README.zh-CN.md", "LICENSE", "upstream/LICENSE", "runtime/python/LICENSE.txt", "vendor/cjson/LICENSE"):
            if (ROOT / name).exists():
                z.write(ROOT / name, name.replace("/", "-"))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (dist / "checksums.txt").write_text(digest + "  " + archive.name + "\n", encoding="utf-8")
    report = {"plugin_version": VERSION, "upstream_version": "1.7.24", "native": "C ABI 1 / schema 6",
              "archive": archive.name, "sha256": digest, "bytes": archive.stat().st_size,
              "library_bytes": library.stat().st_size, "library_sha256": hashlib.sha256(library.read_bytes()).hexdigest(), "upstream_files_verified": len(manifest["files"]),
              "easycpa_source_version": "0.3.50"}
    (dist / "build-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
