# WorkBuddy Hub for EasyCPA

An independent CLIProxyAPI / EasyCLIProxyAPI provider plugin that vendors the original
[WorkBuddy2API-Hub v1.7.24](https://github.com/ardeyouxipianyi/workbuddy2api-hub/releases/tag/v1.7.24)
modules behind the native C ABI. [中文安装与验证说明](README.zh-CN.md).

## Features

- WorkBuddy international and domestic OAuth through the existing EasyCPA endpoint.
- Account-bound model discovery: `workbuddy-intl/<model>` and `workbuddy-cn/<model>`.
- Chat Completions, streaming, tool calls, and the host's Responses conversion.
- Credential quota queries using the original account billing module.
- Bundled Windows amd64 Python runtime; no additional HTTP gateway listener.

The plugin ID and credential provider are `workbuddy-hub`. The existing `workbuddy`
CodeBuddy plugin can coexist; accounts are not automatically migrated.

## Install

Download `workbuddy-hub_0.1.2_windows_amd64.zip` and `checksums.txt` from
[Releases](https://github.com/daodeqing/cpa-plugin-WorkBuddyHub/releases).
The store package contains `workbuddy-hub.dll` at its root, as required by the official
registry. For manual installation, rename it to `workbuddy-hub-v0.1.2.dll` and place it in
the core's `plugins/windows/amd64` directory. If an earlier store version is pinned in
configuration, select the matching release using the store rather than only copying a
different file. Enable the plugin and use **OAuth → WorkBuddy OAuth**.

The client continues to use the existing API URL and API key. Runtime and account data
reside in `plugins/workbuddy-hub`; keep its `data` directory during upgrades.
Credentials saved by the host contain account references, not upstream access tokens.

## Build and verify

Use Python and Zig 0.13.0: `python -B -X utf8 build.py --zig /path/to/zig`.
The build verifies all 584 vendored files against `upstream-manifest.json` before
compilation. Caches and test data belong in `.work` and `.tools`, which are ignored.

- `python -B -X utf8 tests/test_worker.py`: original upstream modules with simulated accounts.
- `python -B -X utf8 tests/prepare.py`: isolated core and legacy plugin test prerequisites.
- `python -B -X utf8 tests/integration.py`: actual DLL and v8.0.38 core, simulated upstream.
- `python -B -X utf8 tests/login_browser.py`: Chromium CSP redirect regression; requires
  Playwright, cryptography, and Microsoft Edge. The test uses an isolated loopback TLS fixture.

Version 0.1.2 passed 28 unit tests, 39 integration checks and four browser checks.
Two user-authorized international accounts exposed 23 models each, returned actual
credit balances, and an actual `hy3` request completed. Domestic account billing and
generation remain covered by simulated tests; real domestic authorization completion
has not been verified. Model discovery is not a guarantee of account entitlement.

## Licenses and provenance

Adapter: [MIT](LICENSE). Original upstream and bundled Python files are kept unchanged;
their licenses and the cJSON MIT license are included in the installation archive.
See `upstream-manifest.json` and `third-party.json` for sources and hashes.
No dashboard, automatic check-in, growth tasks, or client configuration modifications
are started by this adapter.
