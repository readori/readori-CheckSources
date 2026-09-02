# Readori Cloudflare 控制面 + AMD Micro 执行器

该目录把验证器拆成两个运行平面：Cloudflare Workers/Pages 提供公开 Web 控制台、任务状态、D1 租约和 R2 制品；`server/amd_micro_executor.py` 在甲骨云 AMD Micro 上以单并发运行现有 Python/QuickJS/Node 验证核心。AMD Micro 只有 1GB RAM，不能按本地 GUI 的 16 workers 配置运行。

## Cloudflare 资源

- Worker/Pages Static Assets：`public/index.html` 控制台。
- D1：`migrations/0001_init.sql` 中的任务、逐源摘要和事件。
- R2 `INPUTS`：上传的书源 JSON。
- R2 `RESULTS`：完整通过书源结果 JSON。
- D1 `jobs` 表：执行器通过单条原子 UPDATE 获取最旧的排队任务，并用租约过期时间恢复崩溃任务。
- 不再要求 Cloudflare Queue HTTP Pull Token。Queue 可以保留作历史资源，但新任务分发以 D1 租约为唯一来源。

## 部署

```bash
cd cloudflare
npm install
npx wrangler d1 create readori-source-validator
# 将返回的 database_id 填入 wrangler.toml
npx wrangler d1 migrations apply readori-source-validator --remote
npx wrangler r2 bucket create readori-source-validator-inputs
npx wrangler r2 bucket create readori-source-validator-results
npx wrangler secret put EXECUTOR_TOKEN
# 可选：限制浏览器来源
npx wrangler secret put FRONTEND_ORIGIN
npx wrangler deploy
```

### 专用部署仓库的一键 CI

`test-env-setup` 仓库中的 `.github/workflows/deploy-readori-source-validator-cloudflare.yml` 是唯一的自动部署入口，只有 GitHub Actions 页面上的 `workflow_dispatch` 会触发，不响应 push、Pull Request 或定时器。工作流会从 `readori/readori-CheckSources` 拉取指定分支/标签/提交，校验 `cloudflare/` 文件，再执行 Wrangler dry-run；`deploy-and-migrate` 模式会按 `bootstrap_resources` 选项创建缺少的 D1、R2 bucket，应用远程 D1 migration，部署 Worker/Pages 静态控制台并更新加密 Worker secrets。可选 `health_url` 会在部署后执行重试健康检查。

工作流的部署 job 绑定 GitHub `production` environment；建议在
`readori/test-env-setup` → Settings → Environments → `production` → Environment secrets
中配置敏感值。仓库级 Secrets 也兼容，但不要把这些值放到 Variables：

- `TARGET_REPO_PAT`：只读访问 `readori-CheckSources` 的 fine-grained token；
- `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`：具备 Worker、D1、R2 所需权限；
- `EXECUTOR_TOKEN`：对应 Worker 的 `wrangler secret put`，必须与 AMD Micro 的 `READORI_AMD_EXECUTOR_TOKEN` 完全一致；公开控制面无需浏览器 API Key；
- 可选 `CLOUDFLARE_D1_DATABASE_ID`、`FRONTEND_ORIGIN`，不配置时工作流会从 Cloudflare 列表解析；
- 手动运行时填写 `source_ref`、`mode`、`bootstrap_resources` 和可选 `health_url`。

工作流不会调用源仓库的 Actions，也不会把 PAT、Worker token 或 Queue token 写入日志。首次部署前先以 `dry-run` 检查权限和资源名称，再运行 `deploy-and-migrate`；如果非 dry-run 日志提示 `EXECUTOR_TOKEN` 缺失，说明该 Secret 未创建、名称拼写不一致，或被放到了未绑定的环境；AMD Micro 执行器仍需在服务器上单独安装和配置。

资源清单由工作流通过 Cloudflare 官方 API 的 JSON 端点读取，以兼容 Wrangler 4.30+（`wrangler d1/r2/queues list` 不接受 `--json`）；创建和部署仍由 Wrangler 执行。AMD 主机只需要 Worker URL 和 `EXECUTOR_TOKEN`，不需要账户级 Queues Token。

## AMD Micro 执行器

在 Ubuntu/Debian 上只运行一个 systemd 进程，避免同时启动 FastAPI 和 GUI：

```bash
python3 -m venv /opt/readori-validator/.venv
/opt/readori-validator/.venv/bin/pip install -r /opt/readori-validator/server/requirements.txt
export READORI_VALIDATOR_EXECUTOR_PROFILE=amd-micro
export READORI_AMD_EXECUTOR_BASE_URL=https://validator.example.com
export READORI_AMD_EXECUTOR_TOKEN='same-as-cloudflare-secret'
export READORI_AMD_EXECUTOR_ID='amd-micro-01'
/opt/readori-validator/.venv/bin/python -m server.amd_micro_executor --work-dir /var/lib/readori-validator
```

Ubuntu/Debian 可直接运行 `server/install_amd_micro.sh` 完成依赖、专用 `readori` 用户、Python 虚拟环境和 systemd 服务安装。脚本是非交互的，先在当前 shell 导出以下变量，再用 root 执行；变量只写入 `/etc/readori-validator/amd-micro.env`（0600），不会显示在输出中：

```bash
export READORI_AMD_EXECUTOR_BASE_URL='https://validator.example.com'
export READORI_AMD_EXECUTOR_TOKEN='same-as-wrangler-EXECUTOR_TOKEN'
sudo --preserve-env=READORI_AMD_EXECUTOR_BASE_URL,READORI_AMD_EXECUTOR_TOKEN \
  bash server/install_amd_micro.sh
```

可选 `READORI_INSTALL_DIR`、`READORI_AMD_EXECUTOR_ID`、`READORI_AMD_WORK_DIR`、`READORI_AMD_POLL_SECONDS` 和 `READORI_SKIP_SYSTEMD=1`。1GB AMD Micro 默认单并发、每域名并发 1、最多两轮复测；脚本不会把 FastAPI/GUI 作为第二个常驻进程启动。

推荐给实例配置 1–2GB swap 作为 OOM 兜底；执行器默认 D1 租约 12 小时、每源单并发、每域名并发 1。完整链路仍要求搜索→详情→目录→正文，不能因为连通性成功就把书源标记为可用。

## Worker API

公共接口（无需 API Key；由 `PUBLIC_CONTROL_PLANE=true` 开启）：

- `POST /api/uploads`：上传 JSON，返回 `inputKey`。
- `POST /api/jobs`：传 `sources` 或已上传的 `inputKey` 创建任务。
- `GET /api/jobs/:id`、`/sources`、`/events`、`/result`：查询状态、摘要、事件和结果。
- `POST /api/jobs/:id/cancel`、`/resume`：取消或断点恢复。

执行器内部接口（`EXECUTOR_TOKEN` + `x-executor-id`）：

- `POST /internal/jobs/:id/claim`：原子租约，防止 Queue 重投导致重复运行。
- `POST /internal/next`：AMD 执行器原子获取最旧排队任务；同一执行器有未过期租约时返回该任务用于断点恢复。
- `GET /internal/jobs/:id/input`：读取 R2 输入。
- `POST /internal/jobs/:id/progress`：批量上报阶段、源摘要和事件并续租。
- `POST /internal/jobs/:id/result`：流入最终 JSON 到 R2 并完成任务。
- `POST /internal/jobs/:id/fail`、`/cancelled`：处理失败重试和取消。

## 运行边界

Cloudflare 控制面可以水平扩展，但 AMD Micro 执行器故意不扩容。要提速，只增加第二台执行器并使用独立 `executorId`，不要提高单台 AMD Micro 的线程数。部署后仍需用真实书源抽样检查 iOS 搜索、详情、目录和正文链路。D1 租约接口替代了 Queue HTTP Pull，因此执行器不会因 Queue API 权限或 Token 轮换而停止取任务。
