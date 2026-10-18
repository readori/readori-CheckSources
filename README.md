# Readori Source Validator for Windows

这是一个可脱离 Readori iOS 工程运行的书源验证工具，包含命令行验证器、Windows 图形界面、静态书源审计工具和 Windows 打包脚本。

## 功能

- 按四阶段流水线验证：自动去重 → 快速扫描（连通性、搜索入口）→ 完整链路（详情、目录、正文）→ 稳定性复测。
- 去重分三层执行：规范化站点 URL（合并协议、大小写、端口、路径和查询顺序差异）→ 规则指纹（保留同站点的不同有效规则）→ 完整验证后的书名/作者聚合（仅合并同一站点解析到同一本书的变体，避免误删其他站点覆盖）。
- 快速扫描默认每源 8 秒硬截止（可用 `--quick-timeout` 调整到 5–10 秒）；完整验证和复测也有独立硬超时，坏源不会拖死整批任务。
- 每个阶段使用独立并发线程池，阶段之间串行传递结果，避免 1000+ 书源同时进行深链路请求。
- 快速扫描会缓存候选书 URL、规则变量和 Cookie，完整验证复用缓存避免重复搜索；稳定性复测重新搜索以发现临时失效。
- 支持并发、复测轮次、失败原因、阶段明细和通过书源 JSON 输出。
- 兼容 Legado/Readori 常见 CSS、XPath、JSONPath、内嵌 JavaScript、动态请求、Cookie、压缩包和部分 WebView/付费源识别。
- GUI 支持选择 JSON 文件或目录、实时进度、取消任务、打开报告目录。
- 按 App 版本判定（`--app-version 1.1.1|1.1.2|both`，默认 1.1.1），见下文“App 版本档案”。
- `static-tools/capture.py` 提供静态审计、修复、合并和书源生成辅助，不代替联网验证。

## 快速运行（已有 Python）

1. 安装 Python 3.12 或更高版本。
2. 双击 `run_gui.vbs` 打开无控制台图形界面；它会首次自动创建 `.venv` 并安装运行依赖。
3. 也可以先双击 `install_dependencies.ps1`，再双击 `run_gui.vbs`。
4. 选择书源 JSON 文件或目录，设置并发数和超时，点击“开始验证”。

命令行运行示例：

```powershell
python .\source_validator_cli.py `
  --input .\sources.json `
  --report-path .\output\validation_report.json `
  --validated-output .\output\validated_sources.json `
  --validated-output-full .\output\validated_sources_full.json `
  --pipeline staged --quick-timeout 8 `
  --rounds 1 --min-pass-rounds 1 --workers 20 `
  --source-timeout 30 --idle-timeout 180 --no-mirror `
  --app-version 1.1.1
```

## App 版本档案（1.1.1 / 1.1.2 / both）

验证器回答的是“这条书源在某个 Readori App 版本里能不能用”。App 1.1.1（当前公开版）与 1.1.2（下一版）在四处行为不同，验证器按所选档案模拟：

| 差异 | 1.1.1 | 1.1.2 |
| --- | --- | --- |
| `@js:` 开头的 searchUrl | 请求脚本里第一个 http(s) 字面量并丢弃脚本（取不到字面量才执行脚本） | 执行脚本，返回值才是搜索地址 |
| String 字段写成 JSON 数组 / 对象（exploreUrl、loginUi、规则子字段等） | 整个字段丢弃（视为不存在） | 存成紧凑 JSON 文本 |
| CryptoJS | 无条件装一个简化的假对象，会遮挡书源自带的库；`AES.encrypt` 不存在 | 书源引用且未自带时才装 crypto-js 4.1 兼容实现 |
| 批量验证中被拦下的登录 / 验证交互 | 记为失效 | 记为“需要登录/验证”，不算失效 |

三种模式：

- `--app-version 1.1.1`（**默认**）：按当前公开版判定。通过的源在 1.1.1 里确实可用；输出文件路径和含义与以前相同。
- `--app-version 1.1.2`：按下一版判定。输出路径不变，内容为 1.1.2 通过集。
- `--app-version both`：两版对比。1.1.1 先跑全部书源，只有“档案敏感”的源（静态特征命中上表四项之一）才在 1.1.2 下重跑，其余直接复用 1.1.1 的结果。`--pipeline legacy` 不能与 `both` 同用。

GUI 里的“App 版本”下拉框对应这三种模式（1.1.1（当前公开版）/ 1.1.2（下一版）/ 两版对比），默认 1.1.1。服务器任务用配置项 `config.app_version`（`1.1.1` 或 `1.1.2`，默认 1.1.1，Cloudflare 控制台有同名下拉框）；服务器不支持 `both`，想对比请提交两个任务。

输出文件：

- `--validated-output` 与 `--validated-output-full`：`both` 模式下仍是 1.1.1 通过集，默认消费方不受影响。
- `both` 模式额外写 `<文件名>.app-1.1.2<后缀>`（例如 `validated_sources.app-1.1.2.json` 和 `validated_sources_full.app-1.1.2.json`），内容为 1.1.2 通过集。
- 报告（`--report-path`）只增不减：每条结果带 `appVersion`、`appVerdict`（`pass`/`invalid`/`needsInteraction`/`timeout`），`both` 模式另有兼容性标签（如“两版均可用”“仅 1.1.2 可用”“1.1.1 失效、1.1.2 需要登录/验证”）和 `passedSourcesByAppVersion`。

分组与标签（写进书源的 `bookSourceGroup` / `customTag`）：

- 通过源的 `customTag` 为 `✅ 完整验证通过（书籍+详情+目录+正文）`。App 的 `searchValidationTier` / `healthStatus` 只认含“完整验证通过”的标签（排序权重 4，健康状态 healthy）；旧标签 `✅ 书籍+详情+目录+正文通过` 在 App 里不被识别。
- `bookSourceGroup` 为验证日期 `MMDD`（洛杉矶时区）。1.1.2 档案下，档案敏感且未在 1.1.1 通过的源会在 `MMDD` 后追加分组 `需1.1.2`：把 1.1.2 的包导入 1.1.1 时可以按这个分组筛掉；1.1.2 正式上架后可批量删除该分组。分组不含“失效”，不影响健康判定。

已知偏差（验证器与 App 不完全一致的地方）：

- 本机验证 JS 默认走 Node；没有安装 QuickJS 时 QuickJS 路径只能靠单元测试和 CI 覆盖。
- DES / 3DES 的 CTR 模式（CommonCrypto 支持）在验证器里返回 `!unsupported mode`：加密抛错、解密得空。AES 全部模式、DES / 3DES 的 CBC / ECB / CFB / OFB 与 App 一致。
- `both` 模式靠静态特征判断是否重跑。动态拉取的脚本只有写成 `eval(java.ajax/get/cacheFile …)` 或 `java.importScript(` 才会被识别为 CryptoJS 敏感，其他动态代码可能漏判；需要完全确定时请分别用 `1.1.1` 和 `1.1.2` 各跑一遍。
- 静态预判（脚本里出现 `getVerificationCode(` / `openUrl(`，或 loginCheckJs 里出现 `startBrowserAwait(`）仍会提前判失败，只会偏保守（误判失败，不会误判通过）。`openUrl` 在 App 里不被记录，所以 1.1.2 下也记失效；`getVerificationCode` 与 loginCheckJs 里的 `startBrowserAwait` 在 1.1.2 下记“需要登录/验证”。
- 切到 1.1.1 档案后，一部分以前“通过”的源会变成失败，这是预期结果：例如 `@js:` 带字面量的源（已发布通过包里有 5 个），1.1.1 实际会请求字面量、丢掉 POST body；依赖 `startBrowserAwait` 的源，批量验证没有人手完成验证，在 1.1.1 里记失效。
- 规则对象被存成 JSON 字符串（`"ruleSearch":"{...}"`）时，两档案都按 App 的规则解开，再套简写；有这种写法的源结果可能与旧版验证器不同。
- 服务器版不支持 `both`；任务配置存在已有的 `config_json` 里，不改 SQLite 表结构，旧任务没有 `app_version` 时按默认版本处理。

默认版本是 1.1.1；1.1.2 正式上架当天才把 `validator/app_profiles.py` 的 `DEFAULT_APP_VERSION` 改成 1.1.2（同步守卫），不要提前改。

## 生成独立 EXE

在联网的 Windows 电脑上运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_windows.ps1
```

脚本会创建本目录 `.venv`、安装依赖和 PyInstaller，生成：

- `dist/ReadoriSourceValidator.exe`：图形界面；
- `dist/ReadoriSourceValidatorCLI.exe`：命令行版本。

两个 EXE 需要放在同一目录。双击 `ReadoriSourceValidator.exe` 不会显示 CMD 窗口。首次构建需要下载 Python 包；没有网络时可先使用已有虚拟环境运行源码版。

## GitHub Actions 自动构建与 Releases

仓库工作流 `.github/workflows/build-windows-source-validator.yml` 仅支持 Actions 页面手动运行，不会因分支推送、Pull Request 或标签推送自动执行；每次运行都会编译 Windows x64 GUI/CLI EXE。勾选 `publish_release` 后，ZIP 和 `SHA256SUMS.txt` 会上传到 GitHub Release；Release 资产不会按 30 天自动过期，只有手动删除 Release 或资产后才会消失。单独的 Actions 构建附件仍受仓库的 Artifact 保留策略管理。

发布示例：

```powershell
git tag source-validator-v1.0.0
git push origin source-validator-v1.0.0
```

然后在 GitHub Actions 中选择 `Build Windows Source Validator` 并运行。`publish_release` 默认开启：选择已有的 `source-validator-v*` 标签会更新该 Release；从分支运行且不填写 `release_tag` 时，会自动生成 `source-validator-v0.0.<运行编号>` 并发布到当前提交。填写 `release_tag` 可指定一个已有或待创建的 `source-validator-v*` 标签；仅需要构建附件时才取消勾选 `publish_release`。

Release ZIP 内含 `ReadoriSourceValidator.exe`、`ReadoriSourceValidatorCLI.exe`、README 和运行依赖清单；不包含书源数据、Cookie 或其他用户文件。

## 服务器验证服务

服务器版位于 `server/`，网页只负责提交任务和读取进度，实际请求与 Legado 规则解析在后台 worker 执行。它使用 SQLite WAL 保存去重结果、四阶段检查点、事件日志和可恢复任务，并提供每域名并发限制、瞬时网络失败重试及设备兼容性门槛；Cloudflare 控制台按公开模式运行，不要求用户输入 API key：

```powershell
python -m pip install -r .\server\requirements.txt
$env:READORI_VALIDATOR_API_KEY = "change-this-key"
python -m server.source_validator_server --host 0.0.0.0 --port 8787
```

接口顺序为 `POST /v1/jobs` 或 `/v1/jobs/upload`、轮询 `/v1/jobs/{id}` 与 `/events`、按需 `/cancel`/`/resume`，最后从 `/result` 下载仅通过搜索→详情→目录→正文及设备门槛的书源。配置项、反向代理/TLS、部署隔离和抽样真机复测要求见 [`server/README.md`](server/README.md)。

### Cloudflare Pages/Worker + AMD Micro server-only

`cloudflare/` 提供静态控制台和到 AMD Micro FastAPI 的 HTTPS 反向代理。Worker 不再绑定或调用 D1、R2、Queue；任务状态、输入、结果和检查点全部保存在 AMD Micro 的 SQLite/文件系统。AMD Micro 直接运行 `python -m server.source_validator_server`，画像固定 `workers=1`、每域名并发 1、最多两轮稳定性复测，避免 1GB 内存被浏览器参数压垮。部署、服务器 token、TLS、systemd 和公网限流见 [`cloudflare/README.md`](cloudflare/README.md)。

专用部署仓库 `readori/test-env-setup` 的 `deploy-readori-source-validator-cloudflare.yml` 仅手动触发，并从 `readori/readori-CheckSources` 拉取源代码后执行 Cloudflare dry-run/迁移/部署；服务器端可直接运行 [`server/install_amd_micro.sh`](server/install_amd_micro.sh) 完成 AMD Micro 的一键安装配置。

## 参数建议

- 最快首轮筛选：`--quick-timeout 8 --rounds 1 --min-pass-rounds 1 --source-timeout 30`。
- 稳定复测：只对快速扫描和首次完整验证均通过的源运行 `--rounds 3 --min-pass-rounds 2`；后续复测会重新执行搜索、详情、目录、正文。
- `--pipeline legacy` 可临时回退到旧版完整轮次调度，便于结果对比（只能配合单个 App 版本，不能配合 `both`）。
- `--app-version` 默认 1.1.1（当前公开版）；准备发布 1.1.2 前用 `both` 看哪些源两版表现不同。
- `--workers` 不宜盲目增大；建议先使用 8–20，并观察目标站点是否限流。
- `--source-timeout 0` 表示不限制，不建议对大批源使用。

报告中的结果应区分：通过、规则/网络失败、超时、需要交互、WebView/付费内容和环境错误。验证码、登录、Cloudflare、付费章节等需要人工操作的来源不会被工具伪报为通过。

## 依赖和安全边界

运行依赖见 `requirements-validate-sources.txt`。QuickJS、Node.js、py7zr、rarfile 和 cryptography 用于增强兼容性；缺失时对应能力会降级或标记失败。RAR 解包通常还需要系统安装 7-Zip/UnRAR。

书源内嵌 JavaScript 属于不可信输入。服务器部署时应放在隔离容器、限制 CPU/内存/网络和文件访问，禁止把 Cookie、API Key 等秘密写入报告或日志。

## 与 iOS 验证器的关系

本目录中的 Python 核心是服务器/Windows 侧验证实现；iOS 的 Swift 验证器仍负责 App 内的运行时语义和 UI。两者应通过固定 JSON/响应夹具做差异回归，不应直接复制 Swift 文件到 Windows。

## 当前限制

- 纯浏览器页面不能可靠地直接执行完整验证；浏览器应作为服务器任务控制台。
- 必须登录、验证码、WebView 或付费内容的来源只能得到“需要交互”状态。
- 本发行目录提供完整源码和构建脚本；实际在线验证结果取决于运行电脑的网络、DNS、TLS、IP 地域和目标站点状态。
