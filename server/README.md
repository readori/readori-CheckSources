# Readori Server Source Validator

这是 Windows GUI/CLI 验证器的服务器版控制台。网页只负责上传书源、创建任务、查看阶段进度和下载结果；网络请求、Legado 规则执行和 JavaScript 解析全部在服务器 worker 中完成，避免浏览器 CORS、Cookie、请求头和 WebView 限制。`core.py` 暴露身份去重和设备门槛，`worker.py` 暴露持久化队列，`api.py` 暴露 FastAPI 工厂；三者共享 `source_validator_server.py` 的稳定实现，便于后续拆分为独立进程而不改变验证语义。

## 安装与启动

```powershell
cd D:\Readzen\readori-shuyuan
.\.venv\Scripts\python.exe -m pip install -r .\server\requirements.txt
$env:READORI_VALIDATOR_API_KEY = "change-this-key"
$env:READORI_VALIDATOR_DB = "D:\Readzen\readori-shuyuan\server\data\validator.sqlite3"
.\.venv\Scripts\python.exe -m server.source_validator_server --host 0.0.0.0 --port 8787
```

未设置 API key 时仅适合本机开发；部署到局域网或公网前必须设置 `READORI_VALIDATOR_API_KEY`，并在反向代理启用 TLS。`input_path` 始终被限定在一个根目录内：`READORI_VALIDATOR_INPUT_ROOT`，未设置时默认为服务数据目录（`READORI_VALIDATOR_DB` 所在目录）下的 `inputs/` 子目录。相对路径按该根目录解析，路径会先 `resolve`（折叠 `..`、跟随符号链接 / junction）再判断是否仍在根目录内，越界返回 403；只接受文件，不接受目录。

### AMD Micro 一键安装

当前 AMD Micro 使用 server-only 模式，直接运行 FastAPI 和本地 SQLite 验证队列，不启动 D1 租约执行器、GUI 或多并发任务。Ubuntu/Debian 服务器可使用 `install_amd_micro.sh` 自动安装 Python/Node/7-Zip 依赖、创建低权限 `readori` 用户、建立虚拟环境、写入 0600 环境文件并启用 systemd：

如果 SSH 端只需要下载 `server/`，安装器会在执行时从同一仓库和分支自动补齐 `validator/`、`requirements-validate-sources.txt` 与 `requirements.txt` 四个最小运行文件，不会检出或复制仓库其它目录。公共仓库无需 GitHub token；私有仓库可额外设置 `READORI_SOURCE_TOKEN`（只通过请求头使用，不写入命令行或日志）。

只拉取 `server/` 的 SSH 命令：

```bash
set -euo pipefail
mkdir -p /opt/readori-source-validator
git clone --depth=1 --filter=blob:none --sparse --branch main \
  https://github.com/readori/readori-CheckSources.git \
  /opt/readori-source-validator
cd /opt/readori-source-validator
git sparse-checkout set server
```

然后导出下面的运行配置并执行安装脚本：

```bash
cd /opt/readori-source-validator
export READORI_VALIDATOR_API_KEY='same-as-cloudflare-VALIDATOR_SERVER_TOKEN'
export READORI_VALIDATOR_HOST='0.0.0.0'
export READORI_VALIDATOR_PORT='8787'
sudo --preserve-env=READORI_VALIDATOR_API_KEY,READORI_VALIDATOR_HOST,READORI_VALIDATOR_PORT \
  bash server/install_amd_micro.sh
```

如果使用私有仓库或非 `main` 分支，额外设置 `READORI_SOURCE_REPOSITORY=owner/repository`、`READORI_SOURCE_REF=branch-or-tag`，并将 `READORI_SOURCE_TOKEN` 加入 `sudo --preserve-env` 列表。

脚本默认安装到 `/opt/readori-validator`、运行目录 `/var/lib/readori-validator`，服务名为 `readori-source-validator.service`。可通过 `READORI_INSTALL_DIR`、`READORI_AMD_WORK_DIR`、`READORI_VALIDATOR_HOST`、`READORI_VALIDATOR_PORT` 调整；`READORI_SKIP_APT=1` 或 `READORI_SKIP_SYSTEMD=1` 适用于已准备好的主机。密钥只写入 `/etc/readori-validator/amd-micro.env`（0600），不会打印或放进 ExecStart 命令行。安装完成后用 `systemctl status readori-source-validator` 和 `journalctl -u readori-source-validator -f` 检查运行状态。

## API 流程

1. `POST /v1/jobs` 上传 JSON 数组（或传 `input_path`）创建任务；也可使用 `POST /v1/jobs/upload` 的 multipart 文件上传。
2. 轮询 `GET /v1/jobs/{id}` 查看 `queued → quick-scan → full-validation → stability-N → completed` 和 `progress`。
3. `GET /v1/jobs/{id}/sources` 查看去重后的逐源状态，`GET /v1/jobs/{id}/events` 查看实时日志。
4. `POST /v1/jobs/{id}/cancel` 可取消；`POST /v1/jobs/{id}/resume` 从 SQLite 已完成阶段继续，不重复验证已确认的源。
5. `GET /v1/jobs/{id}/result` 只返回完整链路和设备兼容性检查均通过的 Readori 书源 JSON。

## App 版本

任务配置项 `app_version` 决定按哪个 Readori App 版本判定书源：`1.1.1`（当前公开版，**默认**）或 `1.1.2`（下一版）。例如 `POST /v1/jobs` 的 `config` 里带 `{"app_version": "1.1.2"}`；Cloudflare 控制台的“App 版本”下拉框传的就是它。注意 `profile` 仍然表示 `device|legacy`，与 App 版本无关。

- 服务器不支持 `both`。想对比两版，用同一份书源提交两个任务（`1.1.1` 和 `1.1.2`），再比较两份结果。
- 版本只存在已有的 `jobs.config_json` 里，不改 SQLite 表结构。任务创建前存下的旧任务没有这个键，恢复时按默认版本（1.1.1）处理；不认识的值同样回退到默认版本。
- 每个任务在自己的上下文里运行，各阶段线程池会把当前版本带进工作线程，所以并发的 1.1.1 / 1.1.2 任务互不串档案。
- 快速扫描失败的源，`quick_json` 里会带 `appVersion` 和 `appVerdict`（`pass` / `invalid` / `needsInteraction` / `timeout`）；完整验证失败时同样写进 `quick_json`（带 `appVerdictStage: "full-validation"`）。`needsInteraction` 表示被拦下的是登录 / 验证交互，不算失效。
- `/result` 导出的 `customTag` 为 `✅ 完整验证通过（书籍+详情+目录+正文）`；1.1.2 任务里档案敏感的源，`bookSourceGroup` 的 `MMDD` 后会追加 `需1.1.2`。
- 完成事件文字带上所用版本，例如 `validation completed: 3/5 device-ready sources (App 1.1.1)`。

## 验证策略

- 导入先按规范化 `bookSourceUrl`、默认端口、尾斜杠、查询参数和 URL options 去重；保留所有规则变体，同组按完整度评分依次尝试。
- 快速扫描仅验证连通性和搜索/发现入口，默认每源 8 秒；完整阶段验证详情、目录和正文，默认每源 30 秒；默认再做 1 次稳定性复测（总 2 轮）。
- 每个阶段使用独立线程池；同一域名默认最多 2 个并发请求；瞬时连接/超时/5xx 会指数退避重试，规则缺失、登录验证码、WebView/付费内容不会伪造为通过。
- 设备兼容性门槛要求搜索、详情、目录、正文四段均成功，目录至少有章节、正文预览至少 20 个字符，且不能依赖交互式浏览器或延迟 WebView 内容。
- SQLite 使用 WAL；进程重启后仍可从 `job_sources` 的 quick/full/stability 检查点恢复。Cookie、Token、API key 不写入事件日志；完整结果仅通过鉴权接口返回给受信客户端。

## 配置建议

1000+ 书源建议 `workers=8–16`、`domain_concurrency=2`、`quick_timeout=8`、`source_timeout=30`、`rounds=2`。服务器验证通过后仍应在 iOS 真机抽样搜索 → 详情 → 目录 → 正文复测；必须登录、验证码或付费的书源会标为不可自动认证，而不是进入 App 默认可用列表。

## 安全（R145a）

| 项目 | 行为 |
| --- | --- |
| 访问鉴权 | 设置了 `READORI_VALIDATOR_API_KEY` 后，除 `/healthz` 外都要求 `x-api-key` 或 `Authorization: Bearer`，常量时间比较。公网只应经 Cloudflare Worker（它另有访问令牌）访问本服务。 |
| SSRF（来源地址） | 每个书源的 `bookSourceUrl` / `searchUrl` / `exploreUrl` 中的主机，解析 DNS 后只要有一个地址是私网、回环、链路本地（含 169.254.169.254 云元数据）、CGNAT、多播、保留地址，或 IPv4 嵌在 IPv6（`::ffff:`、NAT64、6to4）里的上述地址，该源在 quick-scan 前直接失败，原因 `blocked: non-public address (...)`，不会发出任何请求。结果缓存 5 分钟。无法解析的主机交给引擎，按连通性失败处理。 |
| 本地开发开关 | `READORI_VALIDATOR_ALLOW_PRIVATE_NETWORK=1`（也接受 `true/yes/on`）关闭上述过滤，只用于本机夹具测试，**不要**写进生产环境文件。 |
| 引擎内部请求 | `fetch_text`、JS `ajax`、重定向每一跳都经 `net_guard` 的受保护 Session 检查，连接固定到刚验证过的地址（防 DNS 重绑定）；每个任务在 `network_policy_scope(False)` 内运行。经 HTTP 代理时只能检查解析结果，出口防火墙仍应保留。 |
| 上传大小 | `READORI_VALIDATOR_MAX_UPLOAD_BYTES`（默认 64 MiB）。ASGI 层先看 `Content-Length`，再对分块 / 说谎的请求体逐块计数，超过上限 + 1 MiB 的 multipart 开销立即 413，不等整份读完；文件本身再按分块读取，超过上限即刻 413。 |
| 取消排队任务 | 取消 `queued` 任务立即变为 `cancelled`，worker 取到它时直接跳过；之前 worker 启动时会清掉取消标志，取消请求会丢失。取消后仍可用 `/resume` 重新排队。运行中的任务仍用轮询取消标志。 |
| 每域名并发 | 信号量按域名保存容量，只在请求的上限改变时重建；之前用“剩余许可数大于上限”判断，繁忙时降低上限不生效。 |
| 日志脱敏 | 事件 / 明细里含 `cookie`、`token`、`password`、`secret`、`api key`、`authorization`、`auth`、`credential`、`session` 的键会被替换为 `[redacted]`；`author` / `authors` / `authority` 不再被误伤。 |
| 设备门槛 | `detail_is_device_compatible`（搜索 / 详情 / 目录 / 正文四段、章节数、正文预览长度、交互验证、WebView 延迟内容、搜索页落地检查）只在服务器 `device` profile 下生效；命令行与 GUI 没有，三条路径结果可能不同。 |
| 书源 JS | Node 以权限模型（`--permission`）运行：禁止文件读写（只读运行器临时目录）、子进程、worker、原生模块；环境变量最小化，拿不到服务器令牌；运行前删除 `fetch` / `process` / `require` 等宿主入口。`vm` 与宿主仍同进程，服务器仍应用低权限用户、`ProtectSystem=strict`、限制出口网络运行。 |
