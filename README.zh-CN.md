# WorkBuddy Hub for EasyCPA

直接复用 WorkBuddy2API-Hub v1.7.24 源码，为 EasyCPA 增加 WorkBuddy OAuth 和模型调用。
内部 ID 为 workbuddy-hub，与现有 CodeBuddy Intl CPA 的 workbuddy ID 分开。

## 安装

发布地址：https://github.com/daodeqing/cpa-plugin-WorkBuddyHub/releases。
仅支持 Windows amd64。插件商店收录后可以直接安装；也可以从 Release 下载 ZIP 本地安装。
手动安装时，退出 EasyCPA 并确认内核退出，将 ZIP 根目录中的 workbuddy-hub.dll
重命名为 workbuddy-hub-v0.1.2.dll 后放入内核
plugins/windows/amd64 目录。启动 EasyCPA，在“插件”中启用 WorkBuddy。
插件依赖 EasyCPA 自带的原生插件支持；首次解包发生在第一次实际操作时。

内核安装目录下的 plugins/workbuddy-hub/data 保存账号和用量；升级时保留该目录。
Windows 会把 DLL 复制到临时目录加载；本插件不会把账号保存在该临时副本旁。
Python 和原版源码已包含在 DLL 内，不使用开发目录、Anaconda 或 Codex 运行时。

## 登录与调用

进入 OAuth → WorkBuddy OAuth → 开始登录 → 在浏览器中打开。
选择国际版或国内版，随后在对应官方页面登录，再返回 EasyCPA 等待完成。
可以重复登录多个账号。登录等待时间为 5 分钟；超时后重新发起。
取消 EasyCPA 登录会停止宿主轮询；未使用的后台票据在有效期结束后清理。

客户端继续使用 EasyCPA 原有 API 地址、API Key。
默认 Base URL：http://127.0.0.1:8317/v1。插件本身不监听其他端口。
模型分别为 workbuddy-intl/原模型名 和 workbuddy-cn/原模型名。
只有登录成功的区域会向内核提供模型。模型目录包含上游内置兜底清单；
目录展示不代表账号有套餐权限，也不代表一次真实调用已成功。

提供登录、凭据持久化与刷新、模型列表、Chat Completions、流式工具调用和账号积分查询。
额度复用原版账号模块，并通过内核 QuotaProvider 返回剩余、已用和总积分；查询失败时显示错误。
Responses 使用 EasyCPA 内核的协议转换，不启动原版 8788 网关。
不会启动自动签到、成长任务、更新器或客户端配置写入功能。
账号由 EasyCPA 选择，原版执行器固定使用所选账号与区域，不自动换模型、产品或账号。

## 构建与来源

在插件目录运行 python -B -X utf8 build.py --zig 指向Zig的路径。
先运行 python -B -X utf8 tests/prepare.py 可恢复隔离集成测试的内核与旧插件。
单元测试：python -B -X utf8 tests/test_worker.py。
内核集成：python -B -X utf8 tests/integration.py（开发环境需要 psutil）。
使用 Zig 0.13.0 的 C 编译器；构建脚本将缓存全部放在 .work。
upstream-manifest.json 固定官方 v1.7.24 Release ZIP 的 SHA-256 与逐文件哈希。
upstream/ 和 runtime/ 的原始文件均不修改；适配代码位于 worker.py 和 native/。
cJSON v1.7.19 是 MIT 许可的 JSON 编解码库，见 vendor/cjson/LICENSE。

上游：https://github.com/ardeyouxipianyi/workbuddy2api-hub/releases/tag/v1.7.24
EasyCPA：https://github.com/router-for-me/EasyCLIProxyAPI/releases/tag/v0.3.50

## 验证范围

以 dist/verification-report.json 中的实测记录为准。
0.1.2 已通过 28 项单元测试、39 项 v8.0.38 真实内核加模拟上游的集成检查，
以及 4 项启用 CSP 的 Chromium 浏览器检查。此前 0.1.0 的 6 项界面检查使用
EasyCPA v0.3.50 的真实 OAuth 组件、实际内核元数据和模拟 IPC。
0.1.2 已安装到现用 EasyCPA。用户完成两个国际版账号的授权后，实测每个账号均注册了
23 个模型，积分查询均为剩余 350 / 总计 350（仅为测试时的账号快照，并非固定赠送额度）。
已用 hy3 完成一次正常文本回复；这不保证所有模型对所有账号均有套餐权限。
0.1.1 修复 Chromium 下区域选择表单被 CSP 阻止跳转的问题，改用普通导航链接；
重复点击同一区域会继续原授权，不会重复创建授权。增加启用真实 CSP 的浏览器回归测试：
python -B -X utf8 tests/login_browser.py（需要项目内 .work/qa-python 的 Playwright）。
0.1.2 修复原版模型目录返回二元组、适配层误按字典解析导致模型注册失败的问题，
并增加原版目录格式的回归测试及原生额度能力。两个区域的模拟测试和真实国际版验证分别记录。
