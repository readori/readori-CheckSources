# Readori Cloudflare 控制面 + AMD Micro 执行器

该目录把验证器拆成两个运行平面：Cloudflare Workers/Pages 只处理 Web 控制台、鉴权、任务状态、R2 制品和 Queue；`server/amd_micro_executor.py` 在甲骨云 AMD Micro 上以单并发运行现有 Python/QuickJS/Node 验证核心。AMD Micro 只有 1GB RAM，不能按本地 GUI 的 16 workers 配置运行。

## Cloudflare 资源

- Worker/Pages Static Assets：`public/index.html` 控制台。
- D1：`migrations/0001_init.sql` 中的任务、逐源摘要和事件。
- R2 `INPUTS`：上传的书源 JSON。
- R2 `RESULTS`：完整通过书源结果 JSON。
- Queue `readori-source-validation`：每个验证任务只发送 `{jobId,inputKey,config}` 引用，避免超过消息大小限制。
- 不配置 `[[queues.consumers]]`。执行器使用 HTTP Pull，始终一次拉取一个任务。

## 部署

```bash
cd cloudflare
npm install
npx wrangler d1 create readori-source-validator
# 将返回的 database_id 填入 wrangler.toml
npx wrangler d1 migrations apply readori-source-validator --remote
npx wrangler r2 bucket create readori-source-validator-inputs
npx wrangler r2 bucket create readori-source-validator-results
npx wrangler queues create readori-source-validation
npx wrangler secret put CONTROL_API_KEY
npx wrangler secret put EXECUTOR_TOKEN
# 可选：限制浏览器来源
npx wrangler secret put FRONTEND_ORIGIN
npx wrangler deploy
npx wrangler queues consumer http add readori-source-validation
```

### 专用部署仓库的一键 CI

`test-env-setup` 仓库中的 `.github/workflows/deploy-readori-source-validator-cloudflare.yml` 是唯一的自动部署入口，只有 GitHub Actions 页面上的 `workflow_dispatch` 会触发，不响应 push、Pull Request 或定时器。工作流会从 `readori/readori-CheckSources` 拉取指定分支/标签/提交，校验 `cloudflare/` 文件，再执行 Wrangler dry-run；`deploy-and-migrate` 模式会按 `bootstrap_resources` 选项创建缺少的 D1、R2 bucket 和 Queue，应用远程 D1 migration，部署 Worker/Pages 静态控制台并更新加密 Worker secrets。可选 `health_url` 会在部署后执行重试健康检查。

在 `readori/test-env-setup` 的 Actions secrets/variables 中配置：

- `TARGET_REPO_PAT`：只读访问 `readori-CheckSources` 的 fine-grained token；
- `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`：具备 Worker、D1、R2、Queues 所需权限；
- `CONTROL_API_KEY`、`EXECUTOR_TOKEN`：分别对应 Worker 的 `wrangler secret put`；
- 可选 `CLOUDFLARE_D1_DATABASE_ID`、`FRONTEND_ORIGIN`，不配置时工作流会从 Cloudflare 列表解析；
- 手动运行时填写 `source_ref`、`mode`、`bootstrap_resources` 和可选 `health_url`。

工作流不会调用源仓库的 Actions，也不会把 PAT、Worker token、Queue token 或 API key 写入日志。首次部署前先以 `dry-run` 检查权限和资源名称，再运行 `deploy-and-migrate`；AMD Micro 执行器仍需在服务器上单独安装和配置。

HTTP Pull token需要账户级 Queues 读写权限。不要把该 Token、`EXECUTOR_TOKEN` 或 API Key 写入 Git、前端文件或日志。

## AMD Micro 执行器

在 Ubuntu/Debian 上只运行一个 systemd 进程，避免同时启动 FastAPI 和 GUI：

```bash
python3 -m venv /opt/readori-validator/.venv
/opt/readori-validator/.venv/bin/pip install -r /opt/readori-validator/server/requirements.txt
export READORI_VALIDATOR_EXECUTOR_PROFILE=amd-micro
export READORI_AMD_EXECUTOR_BASE_URL=https://validator.example.com
export READORI_AMD_EXECUTOR_TOKEN='same-as-cloudflare-secret'
export READORI_CF_ACCOUNT_ID='...'
export READORI_CF_QUEUE_ID='...'
export READORI_CF_QUEUE_API_TOKEN='queue-read-write-token'
export READORI_AMD_EXECUTOR_ID='amd-micro-01'
/opt/readori-validator/.venv/bin/python -m server.amd_micro_executor --work-dir /var/lib/readori-validator
```

Ubuntu/Debian 可直接运行 `server/install_amd_micro.sh` 完成依赖、专用 `readori` 用户、Python 虚拟环境和 systemd 服务安装。脚本是非交互的，先在当前 shell 导出以下变量，再用 root 执行；变量只写入 `/etc/readori-validator/amd-micro.env`（0600），不会显示在输出中：

```bash
export READORI_AMD_EXECUTOR_BASE_URL='https://validator.example.com'
export READORI_AMD_EXECUTOR_TOKEN='same-as-wrangler-EXECUTOR_TOKEN'
export READORI_CF_ACCOUNT_ID='...'
export READORI_CF_QUEUE_ID='...'
export READORI_CF_QUEUE_API_TOKEN='...'
sudo --preserve-env=READORI_AMD_EXECUTOR_BASE_URL,READORI_AMD_EXECUTOR_TOKEN,READORI_CF_ACCOUNT_ID,READORI_CF_QUEUE_ID,READORI_CF_QUEUE_API_TOKEN \
  bash server/install_amd_micro.sh
```

可选 `READORI_INSTALL_DIR`、`READORI_AMD_EXECUTOR_ID`、`READORI_AMD_WORK_DIR`、`READORI_AMD_POLL_SECONDS` 和 `READORI_SKIP_SYSTEMD=1`。1GB AMD Micro 默认单并发、每域名并发 1、最多两轮复测；脚本不会把 FastAPI/GUI 作为第二个常驻进程启动。

推荐给实例配置 1–2GB swap 作为 OOM 兜底；执行器默认 `batch_size=1`、Queue 租约 12 小时、每源单并发、每域名并发 1。完整链路仍要求搜索→详情→目录→正文，不能因为连通性成功就把书源标记为可用。

## Worker API

公共接口（`CONTROL_API_KEY`）：

- `POST /api/uploads`：上传 JSON，返回 `inputKey`。
- `POST /api/jobs`：传 `sources` 或已上传的 `inputKey` 创建任务。
- `GET /api/jobs/:id`、`/sources`、`/events`、`/result`：查询状态、摘要、事件和结果。
- `POST /api/jobs/:id/cancel`、`/resume`：取消或断点恢复。

执行器内部接口（`EXECUTOR_TOKEN` + `x-executor-id`）：

- `POST /internal/jobs/:id/claim`：原子租约，防止 Queue 重投导致重复运行。
- `GET /internal/jobs/:id/input`：读取 R2 输入。
- `POST /internal/jobs/:id/progress`：批量上报阶段、源摘要和事件并续租。
- `POST /internal/jobs/:id/result`：流入最终 JSON 到 R2 并完成任务。
- `POST /internal/jobs/:id/fail`、`/cancelled`：处理失败重试和取消。

## 运行边界

Cloudflare 控制面可以水平扩展，但 AMD Micro 执行器故意不扩容。要提速，只增加第二台执行器并使用独立 `executorId`，不要提高单台 AMD Micro 的线程数。部署后仍需用真实书源抽样检查 iOS 搜索、详情、目录和正文链路。
