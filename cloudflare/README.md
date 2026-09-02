# Readori Cloudflare Pages/Worker + AMD Micro 服务器验证器

当前版本是 server-only 架构：Cloudflare Worker/Pages 只提供静态控制台和反向代理，任务、输入 JSON、验证进度、SQLite 检查点和结果全部在 AMD Micro 服务器上处理。Worker 不绑定 D1、R2 或 Queue，验证过程不会消耗 D1 免费额度。

## 架构

```text
浏览器 -> Cloudflare Worker/Pages /api/* -> HTTPS -> AMD Micro FastAPI
                                                    ├─ SQLite 任务/逐源状态/事件
                                                    ├─ 本地输入与结果文件
                                                    └─ 单并发验证核心
```

AMD Micro 只有 1GB RAM，服务固定 `workers=1`、每域名并发 1、最多两轮稳定性复测。Worker 保存 `VALIDATOR_SERVER_TOKEN`，浏览器不会接触服务器 API token。

## AMD Micro 安装

使用 `server/install_amd_micro.sh`。它现在启动 `server.source_validator_server`，不再启动 `amd_micro_executor`，也不需要 Cloudflare Account ID、D1、R2、Queue 或 Queue token。

```bash
cd /opt/readori-source-validator
export READORI_VALIDATOR_API_KEY='与 Worker secret 完全一致的长随机字符串'
export READORI_VALIDATOR_HOST='0.0.0.0'
export READORI_VALIDATOR_PORT='8787'
bash server/install_amd_micro.sh
```

如果仍使用旧变量，`READORI_AMD_EXECUTOR_TOKEN` 会兼容地作为 API key；`READORI_AMD_EXECUTOR_BASE_URL` 已不再需要。安装后确认：

```bash
systemctl is-enabled readori-source-validator.service
systemctl is-active readori-source-validator.service
curl --fail http://127.0.0.1:8787/healthz
journalctl -u readori-source-validator.service -n 50 --no-pager
```

服务器必须通过 HTTPS 对 Worker 可访问。可用 Caddy/Nginx 反代 `127.0.0.1:8787`，只开放反代端口，不要直接暴露 SQLite 或工作目录。

## Worker 配置

`wrangler.toml` 不含任何 D1/R2 配置。部署前设置：

```bash
# Put the AMD Micro HTTPS URL in wrangler.toml [vars].
npx wrangler secret put VALIDATOR_SERVER_TOKEN
npx wrangler deploy
```

`VALIDATOR_SERVER_URL` 必须是 AMD Micro 的 HTTPS 地址，例如 `https://validator.example.com`；`VALIDATOR_SERVER_TOKEN` 必须等于服务器的 `READORI_VALIDATOR_API_KEY`。Worker 公共页面不要求用户输入 API key，但服务器 token 只保存在 Worker secret 和服务器的 0600 环境文件中。

## API 映射

控制台继续使用原来的 `/api` 路径，Worker 转发到服务器 `/v1`：

- `POST /api/jobs` → `POST /v1/jobs`
- `GET /api/jobs/:id` → `GET /v1/jobs/:id`
- `GET /api/jobs/:id/sources`、`/events`、`/result`
- `POST /api/jobs/:id/cancel`、`/resume`
- `POST /api/uploads` → `POST /v1/jobs/upload`

服务器 API 使用本地 `JobStore` 的 SQLite WAL 模式，进程重启后会自动恢复 queued/running/resuming 任务。详细阶段和结果不再写入 D1/R2。

## 公网使用边界

控制台是公开的，任何人都可以创建验证任务；因此必须在 Caddy/Nginx 或 Cloudflare WAF 设置 IP 限流、上传大小限制和任务频率限制。单台 AMD Micro 只适合单并发，不能通过提高线程数提速；需要更高吞吐时应增加第二台服务器，而不是提高 1GB 实例并发。

## 部署工作流

`test-env-setup` 仓库的部署工作流仍只接受 `workflow_dispatch`。它只部署 Worker/Pages 并写入 `VALIDATOR_SERVER_URL`、`VALIDATOR_SERVER_TOKEN`，不再解析、创建或迁移 D1，也不需要 `CLOUDFLARE_D1_DATABASE_ID`、D1 权限或 Queue 权限。
