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

未设置 API key 时仅适合本机开发；部署到局域网或公网前必须设置 `READORI_VALIDATOR_API_KEY`，并在反向代理启用 TLS。可用 `READORI_VALIDATOR_INPUT_ROOT` 限制 `input_path` 只能读取指定目录。

## API 流程

1. `POST /v1/jobs` 上传 JSON 数组（或传 `input_path`）创建任务；也可使用 `POST /v1/jobs/upload` 的 multipart 文件上传。
2. 轮询 `GET /v1/jobs/{id}` 查看 `queued → quick-scan → full-validation → stability-N → completed` 和 `progress`。
3. `GET /v1/jobs/{id}/sources` 查看去重后的逐源状态，`GET /v1/jobs/{id}/events` 查看实时日志。
4. `POST /v1/jobs/{id}/cancel` 可取消；`POST /v1/jobs/{id}/resume` 从 SQLite 已完成阶段继续，不重复验证已确认的源。
5. `GET /v1/jobs/{id}/result` 只返回完整链路和设备兼容性检查均通过的 Readori 书源 JSON。

## 验证策略

- 导入先按规范化 `bookSourceUrl`、默认端口、尾斜杠、查询参数和 URL options 去重；保留所有规则变体，同组按完整度评分依次尝试。
- 快速扫描仅验证连通性和搜索/发现入口，默认每源 8 秒；完整阶段验证详情、目录和正文，默认每源 30 秒；默认再做 1 次稳定性复测（总 2 轮）。
- 每个阶段使用独立线程池；同一域名默认最多 2 个并发请求；瞬时连接/超时/5xx 会指数退避重试，规则缺失、登录验证码、WebView/付费内容不会伪造为通过。
- 设备兼容性门槛要求搜索、详情、目录、正文四段均成功，目录至少有章节、正文预览至少 20 个字符，且不能依赖交互式浏览器或延迟 WebView 内容。
- SQLite 使用 WAL；进程重启后仍可从 `job_sources` 的 quick/full/stability 检查点恢复。Cookie、Token、API key 不写入事件日志；完整结果仅通过鉴权接口返回给受信客户端。

## 配置建议

1000+ 书源建议 `workers=8–16`、`domain_concurrency=2`、`quick_timeout=8`、`source_timeout=30`、`rounds=2`。服务器验证通过后仍应在 iOS 真机抽样搜索 → 详情 → 目录 → 正文复测；必须登录、验证码或付费的书源会标为不可自动认证，而不是进入 App 默认可用列表。
