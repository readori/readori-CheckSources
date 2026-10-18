# Readori Cloudflare Pages/Worker + AMD Micro 服务器验证器

当前版本是 server-only 架构：Cloudflare Worker/Pages 只提供静态控制台和反向代理，任务、输入 JSON、验证进度、SQLite 检查点和结果全部在 AMD Micro 服务器上处理。Worker 不绑定 D1、R2 或 Queue，验证过程不会消耗 D1 免费额度。

## 架构

```text
浏览器 -> Cloudflare Worker/Pages /api/* -> HTTPS -> AMD Micro FastAPI
                                                    ├─ SQLite 任务/逐源状态/事件
                                                    ├─ 本地输入与结果文件
                                                    └─ 单并发验证核心
```

AMD Micro 只有 1GB RAM，服务固定 `workers=1`、每域名并发 1、最多两轮稳定性复测。Worker 保存 `VALIDATOR_SERVER_TOKEN`，浏览器不会接触服务器 API token；浏览器只持有访问令牌 `READORI_VALIDATOR_ACCESS_TOKEN`，且只有访问令牌校验通过的请求才会被注入服务器 token。

## AMD Micro 安装

使用 `server/install_amd_micro.sh`。它现在启动 `server.source_validator_server`，不再启动 `amd_micro_executor`，也不需要 Cloudflare Account ID、D1、R2、Queue 或 Queue token。

```bash
cd /opt/readori-source-validator
export READORI_VALIDATOR_API_KEY='与 Worker secret 完全一致的长随机字符串'
export READORI_VALIDATOR_HOST='0.0.0.0'
export READORI_VALIDATOR_PORT='8787'
bash server/install_amd_micro.sh
```

后续升级如果 `/etc/readori-validator/amd-micro.env` 已存在有效 API key，可不再导出变量，安装器会安全复用该文件中的值；如需轮换密钥，再临时导出 `READORI_VALIDATOR_API_KEY` 覆盖它。

如果仍使用旧变量，`READORI_AMD_EXECUTOR_TOKEN` 会兼容地作为 API key；`READORI_AMD_EXECUTOR_BASE_URL` 已不再需要。安装后确认：

```bash
systemctl is-enabled readori-source-validator.service
systemctl is-active readori-source-validator.service
curl --fail http://127.0.0.1:8787/healthz
journalctl -u readori-source-validator.service -n 50 --no-pager
```

服务器必须通过 HTTPS 对 Worker 可访问。可用 Caddy/Nginx 反代 `127.0.0.1:8787`，只开放反代端口，不要直接暴露 SQLite 或工作目录。

## Worker 配置

`wrangler.toml` 不含任何 D1/R2 配置，并将 `syyz.readori.com` 声明为 Worker Custom Domain。部署前设置：

```bash
# Put the AMD Micro HTTPS URL in wrangler.toml [vars].
npx wrangler secret put VALIDATOR_SERVER_TOKEN
npx wrangler secret put READORI_VALIDATOR_ACCESS_TOKEN   # 必填：控制台访问令牌（长随机字符串，例如 openssl rand -hex 32）
npx wrangler deploy
```

**未设置 `READORI_VALIDATOR_ACCESS_TOKEN` 时 Worker 对所有受保护接口返回 503（`未配置访问令牌`），不会以开放模式运行。**

`VALIDATOR_SERVER_URL` 必须是 AMD Micro 的 HTTPS 主机名，本项目默认使用 `https://syyz-api.readori.com`；不能填写裸 IPv4/IPv6 地址，也不能填写前端 Worker 的 `syyz.readori.com`；`VALIDATOR_SERVER_TOKEN` 必须等于服务器的 `READORI_VALIDATOR_API_KEY`。控制台页面要求填写访问令牌（页面里的“访问令牌”输入框，保存在本浏览器 localStorage，可点“清除令牌”删除）；服务器 token 只保存在 Worker secret 和服务器的 0600 环境文件中，访问令牌与服务器 token 是两个不同的值，不要设成相同。请在 Cloudflare DNS 中为后端主机创建 A/AAAA 记录，并在 AMD Micro 上用 Caddy/Nginx 配置该主机名的公信 TLS 证书后反代到 `127.0.0.1:8787`。

用户访问入口固定为 [`https://syyz.readori.com/`](https://syyz.readori.com/)，接口健康检查为 [`https://syyz.readori.com/api/healthz`](https://syyz.readori.com/api/healthz)。Cloudflare 控制台中已经绑定该 Custom Domain 时，重新部署同一 Worker 即可；不要再为同一主机添加 `/*` Route。

## API 映射

控制台继续使用原来的 `/api` 路径，Worker 转发到服务器 `/v1`：

- `POST /api/jobs` → `POST /v1/jobs`
- `GET /api/jobs/:id` → `GET /v1/jobs/:id`
- `GET /api/jobs/:id/sources`、`/events`、`/result`
- `POST /api/jobs/:id/cancel`、`/resume`
- `POST /api/uploads` → `POST /v1/jobs/upload`

服务器 API 使用本地 `JobStore` 的 SQLite WAL 模式，进程重启后会自动恢复 queued/running/resuming 任务。详细阶段和结果不再写入 D1/R2。

## 访问控制与公网使用边界

- `/api/healthz` 是唯一免令牌接口（只返回服务状态，不带服务器 token）。其余 `/api/*`（创建 / 上传 / 查看 / 事件 / 来源 / 结果 / 取消 / 恢复）都要求 `Authorization: Bearer <READORI_VALIDATOR_ACCESS_TOKEN>`：缺失或错误返回 401，令牌用 SHA-256 摘要做常量时间比较；通过后 Worker 丢弃调用者的 `Authorization` / `Cookie` / `x-api-key`，再注入服务器 token。
- Worker 只放行已知路由和方法（未知路径 404，方法不符 405），并且会删除 `/api/jobs` 请求体里的 `input_path` / `inputPath`，调用者无法借 Worker 读取服务器文件。
- 轮换访问令牌：`npx wrangler secret put READORI_VALIDATOR_ACCESS_TOKEN` 重新写入即可，旧令牌立刻失效，无需重新部署。
- 令牌属于受信任的验证人员；书源 JS 在服务器执行，泄露令牌等于允许对方让服务器访问任意网址并运行脚本。令牌一旦外泄必须立即轮换。

即使有令牌，仍应在 Caddy/Nginx 或 Cloudflare WAF 设置 IP 限流、上传大小限制和任务频率限制（401 重试也应限速以防暴力猜测）。单台 AMD Micro 只适合单并发，不能通过提高线程数提速；需要更高吞吐时应增加第二台服务器，而不是提高 1GB 实例并发。

## 部署工作流

`test-env-setup` 仓库的部署工作流仍只接受 `workflow_dispatch`。它只部署 Worker/Pages 并写入 `VALIDATOR_SERVER_URL`、`VALIDATOR_SERVER_TOKEN`、`READORI_VALIDATOR_ACCESS_TOKEN`（三者在 production 环境缺一不可，缺访问令牌时部署前检查失败），不再解析、创建或迁移 D1，也不需要 `CLOUDFLARE_D1_DATABASE_ID`、D1 权限或 Queue 权限。

## 书源调试工作台（R146）

`public/debug.html`（入口：`https://syyz.readori.com/debug.html`，控制台首页有链接）是单页、原生 JS、无需构建的工作台：载入书源 → 按字段编辑（撤销 / 重做 / 还原，最多 100 步）→ 字段旁 ▶ 离线测规则 → 调试（关键字 / 详情 / `标题::发现` / `++目录` / `--正文`，可单步选书选章）→ 看日志、原始响应、修复建议、差异、前后对比 → 下载 `fixed.json` 与 `fix-report.json`。访问令牌与控制台共用（localStorage 键 `readori-validator-access-token`），收到 401 会自动展开令牌输入框。

流式读取用 `fetch` + `ReadableStream`（`EventSource` 不能带 `Authorization`）手工解析 SSE；断线用 `?after=<最后 seq>` 重连最多 3 次，失败后改为每 2 秒轮询 `/events`。

Worker 新增路由（全部要访问令牌、只放行下表的路径与方法，其余 404 / 405；会话号必须是 8–64 位 `[A-Za-z0-9_-]`，`rawId` 必须是数字）：

| 浏览器 | 转发到服务器 |
| --- | --- |
| `POST /api/debug/sessions` | `POST /v1/debug/sessions` |
| `GET /api/debug/sessions/:id` | `GET /v1/debug/sessions/:id` |
| `GET /api/debug/sessions/:id/stream` | `GET .../stream`（SSE） |
| `GET /api/debug/sessions/:id/events` | `GET .../events` |
| `POST /api/debug/sessions/:id/resume` · `/cancel` | `POST .../resume` · `.../cancel` |
| `GET /api/debug/sessions/:id/raw` · `/raw/:rawId` | `GET .../raw` · `.../raw/:rawId` |
| `GET\|POST /api/debug/sessions/:id/suggestions` | 同名 |
| `POST /api/debug/test-rule` · `/preview` · `/diff` · `/export` | `POST /v1/debug/...` |

实现要点（`src/index.js`）：

- 转发前删除浏览器的 `Authorization` / `Cookie` / `x-api-key`，再注入 `VALIDATOR_SERVER_TOKEN`；GET 保留查询串（`after` / `limit` / `maxBytes` / `source`），POST 不转发查询串。
- 调试 POST 的请求体**原样转发**（不解析、不重新序列化），上限 `MAX_DEBUG_BODY_BYTES = 2 MiB`：先看 `Content-Length`，没有长度的分块请求体边读边数，超限立即 413 并停止读取，不会请求到服务器。
- 响应（包括 SSE）一律以流透传：不 `await response.text()`、不 JSON 解析、不缓冲；`text/event-stream` 附加 `x-accel-buffering: no` 与 `cache-control: no-store`，`Retry-After` 原样带回（429）；`transformPayload` 对 `/v1/debug/` 原样返回，不做批量任务的字段改名。浏览器断开时 `/stream` 请求的 `signal` 会中止到服务器的连接。
- 源站经 Cloudflare 代理时 100 秒没有字节会 524，服务器每 15 秒发一次 `: ping` 心跳覆盖这个空窗。

部署：先在服务器上重跑 `server/install_amd_micro.sh`（新模块和 `server/debug_api.py`），再 `npx wrangler deploy` 部署 Worker 与 `public/`。只更新 Worker 而不更新服务器时，`/api/debug/*` 会得到服务器的 404。
