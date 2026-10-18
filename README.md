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

## 判定规则（与 App 对齐）

验证结论以 Readori App 的实际行为为准：**非空规则的结果原样使用**。

- 正文 / 目录 / 详情 / 搜索的兜底与启发式只在对应规则为空时运行；规则写了但没命中，该阶段就失败，不再靠整页文本、链接猜测、JSON 键猜测或书源名救回。
- 请求只发一次，使用书源自带 Header；不会换默认浏览器 Header 重试。空响应时「无 Cookie 新 Session」重试只用于规则为空且开启 cookie jar 的源；`enabledCookieJar=false` 不保存 Set-Cookie；每个书源独立 Session。
- 文字书源正文规则为空判失败（App 会把章节网址当正文显示）；音频 / 图片书源除外。replaceRegex 或 `##` 把正文清空判失败。
- 执行 `ruleSearch.init`（与 App 一致）；无法识别的规则不再按原文返回。
- 网络错误不再吞掉：failureReason 为 `network error: dns / tls / timed out / connection refused / connection reset / too many redirects / http NNN`，`detail.networkError` 含阶段、类别、URL、状态码。
- 最终正文门槛（去空白后 30 字 / 信号 15）比 App 的「非空即可」略严，用来排除「请登录」之类的占位页。

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

服务器版位于 `server/`，网页只负责提交任务和读取进度，实际请求与 Legado 规则解析在后台 worker 执行。它使用 SQLite WAL 保存去重结果、四阶段检查点、事件日志和可恢复任务，并提供每域名并发限制、瞬时网络失败重试及设备兼容性门槛；Cloudflare 控制台要求填写访问令牌（见下文“安全与部署”），服务器 API key 只保存在 Worker secret 里：

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

### 安全与部署（R145a）

- Cloudflare Worker 对除 `/api/healthz` 以外的所有 `/api/*` 接口要求 `Authorization: Bearer <READORI_VALIDATOR_ACCESS_TOKEN>`；只有鉴权通过才会给转发请求注入服务器 token。Worker 未配置该 secret 时返回 503（fail closed）。上线前必须先 `wrangler secret put READORI_VALIDATOR_ACCESS_TOKEN`，并在 GitHub 的 production 环境配置同名 secret（手动 workflow 才会部署，见 `cloudflare/README.md`）。
- 服务器 `input_path` 永远限定在 `READORI_VALIDATOR_INPUT_ROOT`；未设置时为服务数据目录（SQLite 所在目录）下的 `inputs/` 子目录，`..` 与符号链接都会按真实位置判定。
- 服务器拒绝首个请求地址落在私网 / 回环 / 链路本地 / 云元数据地址的书源（解析 DNS 后检查所有地址）；本地开发需要访问 127.0.0.1 夹具时设置 `READORI_VALIDATOR_ALLOW_PRIVATE_NETWORK=1`。`validator/net_guard.py` 是可复用的纯函数模块（`is_public_address` / `check_url_allowed`）。验证引擎自己发出的请求见下一小节（R145b）。
- 设备兼容性门槛 `detail_is_device_compatible` 只在服务器的 `device` profile 下生效；命令行与 GUI 没有这道门槛，三条路径对同一书源的结果可能不同。
- 书源 JS 在 Node `vm` 中执行；R145b 起外层加了 Node 权限模型等多道防线（见下一小节），但 `vm` 本身仍不是安全边界，服务器部署仍应叠加隔离（低权限用户、systemd 限制、无内网出口）。

### 引擎网络过滤与 Node 沙箱（R145b）

- 引擎内所有出站请求（`fetch_text` / `fetch_bytes`、JS `java.ajax` / `connect` / `get` / `post` / `webView` 等经 Python 桥发出的请求、JS 请求日志重放）都经过 `net_guard.GuardedSession`：请求发出前检查目标，**重定向的每一跳**都重新检查（requests 每一跳都会重新进入适配器），被拒绝时不发请求，`failureReason` 为 `network error: blocked: non-public address`，`detail.networkError.category` 为 `blocked: non-public address`。连接直接拨向「检查时解析出的、已验证为公网」的地址（Host 头 / TLS 主机名不变），因此检查与连接之间没有再次解析的窗口（DNS 重绑定）；经 HTTP 代理访问时由代理解析，只能检查解析结果。
- 策略：命令行 / GUI 默认**允许**私网（本机运行时书源可能在局域网，App 同样能访问）；`--block-private-network` 禁止。服务器每个任务都在 `network_policy_scope(False)` 内运行，进程默认也设为禁止，只有 `READORI_VALIDATOR_ALLOW_PRIVATE_NETWORK=1` 才放开。代码里用 `validator.set_network_policy(bool)`（进程默认）或 `network_policy_scope(bool)`（上下文；新线程需 `copy_context().run` 绑定）。
- Node 子进程：环境变量只保留 `PATH`、`SYSTEMROOT` 等启动必需项（不继承服务器 token / API key，`NODE_OPTIONS` 也不继承）；Node >= 23.5（本机 v24 已验证）加 `--permission` 且不授予 fs 写 / `child_process` / worker / addon，fs 读只开放运行器脚本所在的私有临时目录；旧版本 Node 不支持时降级并向 stderr 警告一次，仅剩环境变量最小化与运行器内的加固。
- 运行器在执行书源代码前去掉宿主域的逃逸入口：宿主 `Function.prototype.constructor`（`constructor.constructor('return process')()`）、`process`、`require`、`console`、`fetch`、`WebSocket`、`Error.prepareStackTrace` 等。Node 权限模型不限制网络，所以这一步同时堵住逃逸后直连内网。
- 残余风险：`vm` 与宿主共用同一进程与 V8 堆，未知的 V8 / Node 逃逸仍可能执行任意 JS（但无 fs 写 / 子进程 / 环境变量，网络入口已移除）；Node 子进程仍可使用本机 CPU / 内存（每次有超时）；`--block-private-network` 之外的 DNS 解析发生在本机解析器，需要出口防火墙作为最后一道防线。

## 书源工作台（R146）

单个书源的编辑、单步调试、修复建议和修复前后对比。批量验证的参数和行为不变；工作台有三个入口，共用同一套引擎（`validator/debug_engine.py` 等），调试走的链路与批量验证一致，只是开着跟踪。

### 命令行子命令

第一个参数是子命令时进入工作台；否则仍是批量验证（批量参数都以 `--` 开头）。`<file>` 是书源 JSON / JSON5；文件里有多条书源时用 `--source URL|名称`（先精确 URL → 规范化 URL → 精确名称 → 名称子串，歧义会列出候选并退出 2）或 `--index N`（从 0 开始）。`--app-version 1.1.1|1.1.2`（默认 1.1.1）；`--jsonl` 每行输出一个 JSON 事件；`--json` 结束时输出摘要 JSON（单独使用时日志改走 stderr）；`--block-private-network` 拒绝私网地址（默认允许，方便调试本机站点）。

| 子命令 | 用途 | 示例 |
|---|---|---|
| `debug` | Legado / App 风格单步调试；`--key` 缺省 = 完整校验，`关键词` / `标题::发现URL` / `详情URL` / `++目录URL` / `--正文URL`；`--step`（终端里在选书 / 选章处暂停）、`--pick-book N`、`--pick-chapter N`、`--timeout 60`、`--save-raw DIR` | `source_validator_cli.py debug sources.json --source 笔趣阁 --key "++https://x/book/1/"` |
| `test-rule` | 在保存的响应上重放一条规则：`--field ruleSearch.name`、`--rule R`（缺省用书源里的）、`--body-file F`（`-` = stdin）或 `--raw DIR [--raw-id N]`（`debug --save-raw` 的目录）、`--base-url`、`--item N`、`--online`（允许规则内 JS 联网，默认离线） | `source_validator_cli.py test-rule sources.json --field ruleToc.chapterList --raw raw/` |
| `fix` | 调试 → 建议 → 预览 → 应用 → 导出：`--apply F5,F7` / `--apply-safe`（全部离线且预览通过的）、`--online-preview`、`--revalidate`、`--keep-source-url`、`--out`、`--report`、`--dry-run`（不写任何文件） | `source_validator_cli.py fix sources.json --source 笔趣阁 --apply-safe --revalidate --out fixed.json --report fix-report.json` |
| `validate-one` | 按批量流水线（快速扫描 → 完整验证 → 稳定复测）校验一个书源：`--quick-timeout 8 --source-timeout 60 --rounds 1`、`--trace`（打印事件）、`--app-version both`（两版各跑一次并给出兼容结论） | `source_validator_cli.py validate-one sources.json --index 3 --trace` |
| `export` | 导出书源；`--from-report fix-report.json` 把报告里的改动应用到对应书源（按 `originalBookSourceUrl` 匹配），`--only-changed`、`--only-passed`、`--keep-source-url`、`--out` | `source_validator_cli.py export sources.json --from-report fix-report.json --only-changed --out fixed.json` |
| `rpc` | stdin / stdout JSON-lines 服务，仅供桌面工作台使用 | |

退出码：0 通过 / 规则有结果；1 书源失效或规则结果为空；2 用法或输入错误；3 需要登录 / 验证；4 超时；130 Ctrl+C。

- `fix` 只导出被选中的那一条书源（`<输入>.fixed.json`，或 `--out`）；修复报告 `fix-report.json`（`readori.fix-report/1`）记录每处改动的路径、旧值、新值、原因和修复前后结论。修复过的书源会写入 `// Fixed: <洛杉矶时间> <建议代码>: <原因>` 注释和 `lastUpdateTime`；不会写验证标签。
- 域名迁移（F1）默认连 `bookSourceUrl` 一起改（它是书源在 App 里的主键，导入后会成为新书源、旧源仍保留；报告里的 `originalBookSourceUrl` 记着旧值）。不想改主键就加 `--keep-source-url`。
- `--apply` 指定的建议只有在预览通过（或联网类建议未被预览否决）时才会应用；被拒绝的会列出原因。
- 日志和时间一律使用洛杉矶时区（夏令时由系统时区库处理）。

### 桌面工作台

批量验证器窗口的「书源工作台…」按钮会打开工作台窗口（单例；批量验证运行时也能用）。左：书源列表（名称 / 上次结论 / 耗时，可过滤，导入 / 导出已修改）；中：按 Legado 编辑器分组的字段（基本 / 搜索 / 发现 / 详情 / 目录 / 正文 / JSON），每个规则字段有提示文案和「▶」单字段测试，工具条有撤销 / 重做 / 还原；右：日志、原始响应、规则测试、建议（预览 / 应用）、差异、对比、帮助。顶栏是调试 key 输入和芯片（校验关键字 / 系统 / 发现 / 详情 / ++目录 / --正文）、调试 / 单步 / 停止、校验此源、修复前后对比、App 版本。

工作台自己不加载校验器：它启动一个常驻子进程 `ReadoriSourceValidatorCLI.exe rpc`（开发时是 `python -u source_validator_cli.py rpc`），UI 线程只写请求，后台线程读响应并经 `after()` 回到 UI 线程。「停止」先发取消请求，3 秒内没有结束就强制结束并重启子进程（已编辑的书源会自动恢复）。`python workbench_gui.py --self-test` 可在没有窗口的情况下检查控件树。

## 与 iOS 验证器的关系

本目录中的 Python 核心是服务器/Windows 侧验证实现；iOS 的 Swift 验证器仍负责 App 内的运行时语义和 UI。两者应通过固定 JSON/响应夹具做差异回归，不应直接复制 Swift 文件到 Windows。

## 当前限制

- 纯浏览器页面不能可靠地直接执行完整验证；浏览器应作为服务器任务控制台。
- 必须登录、验证码、WebView 或付费内容的来源只能得到“需要交互”状态。
- 本发行目录提供完整源码和构建脚本；实际在线验证结果取决于运行电脑的网络、DNS、TLS、IP 地域和目标站点状态。
