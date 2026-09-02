const JSON_HEADERS = { "content-type": "application/json; charset=utf-8" };
const SENSITIVE_KEY = /(cookie|token|password|passwd|secret|api[-_]?key|authorization|auth|credential|session)/i;

function now() {
  return Date.now();
}

function maxInt(value, fallback, minimum, maximum) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(minimum, Math.min(maximum, Math.trunc(parsed)));
}

function maxFloat(value, fallback, minimum, maximum) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(minimum, Math.min(maximum, parsed));
}

function corsHeaders(request, env, extra = {}) {
  const configured = String(env.FRONTEND_ORIGIN || "").trim();
  const requestOrigin = request.headers.get("Origin") || "";
  const origin = configured || requestOrigin || "*";
  return {
    ...JSON_HEADERS,
    "access-control-allow-origin": origin,
    "access-control-allow-headers": "authorization, content-type, x-api-key, x-executor-token, x-executor-id",
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-expose-headers": "content-length, etag",
    "cache-control": "no-store",
    ...extra,
  };
}

function json(request, env, value, status = 200, extra = {}) {
  return new Response(JSON.stringify(value), { status, headers: corsHeaders(request, env, extra) });
}

function textError(request, env, status, message) {
  return json(request, env, { error: message }, status);
}

function bearerOrHeader(request, name) {
  const direct = request.headers.get(name) || "";
  if (direct) return direct.trim();
  const authorization = request.headers.get("authorization") || "";
  return authorization.toLowerCase().startsWith("bearer ") ? authorization.slice(7).trim() : "";
}

function constantTimeEqual(left, right) {
  if (!left || !right || left.length !== right.length) return false;
  let result = 0;
  for (let index = 0; index < left.length; index += 1) result |= left.charCodeAt(index) ^ right.charCodeAt(index);
  return result === 0;
}

function publicAuth(request, env) {
  const expected = String(env.CONTROL_API_KEY || "");
  if (!expected) return { response: textError(request, env, 503, "CONTROL_API_KEY is not configured") };
  if (!constantTimeEqual(bearerOrHeader(request, "x-api-key"), expected)) {
    return { response: textError(request, env, 401, "invalid API key") };
  }
  return {};
}

function executorAuth(request, env) {
  const expected = String(env.EXECUTOR_TOKEN || "");
  if (!expected) return { response: textError(request, env, 503, "EXECUTOR_TOKEN is not configured") };
  if (!constantTimeEqual(bearerOrHeader(request, "x-executor-token"), expected)) {
    return { response: textError(request, env, 401, "invalid executor token") };
  }
  return {};
}

async function readBytes(request, maxBytes) {
  const declared = Number(request.headers.get("content-length") || 0);
  if (declared > maxBytes) throw new Error(`payload exceeds ${maxBytes} bytes`);
  const buffer = new Uint8Array(await request.arrayBuffer());
  if (buffer.byteLength > maxBytes) throw new Error(`payload exceeds ${maxBytes} bytes`);
  return buffer;
}

async function readJSON(request, maxBytes) {
  const bytes = await readBytes(request, maxBytes);
  try {
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch (error) {
    throw new Error(`invalid JSON: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function safeInputKey(value) {
  const key = String(value || "").trim();
  return key.startsWith("inputs/") && key.length < 512 && !key.includes("..") && !key.includes("\\") ? key : "";
}

function normalizeConfig(value) {
  const requested = value && typeof value === "object" ? value : {};
  const rounds = maxInt(requested.rounds, 2, 1, 2);
  return {
    // The executor is the AMD Micro VM.  These values are authoritative and
    // cannot be raised by a browser request.
    workers: 1,
    domain_concurrency: 1,
    quick_timeout: maxFloat(requested.quick_timeout, 8, 1, 15),
    source_timeout: maxFloat(requested.source_timeout, 30, 5, 45),
    rounds,
    min_pass_rounds: maxInt(requested.min_pass_rounds, rounds, 1, rounds),
    idle_timeout: maxFloat(requested.idle_timeout, 180, 15, 300),
    max_retries: maxInt(requested.max_retries, 1, 0, 1),
    profile: "device",
    executor_profile: "amd-micro",
  };
}

function redact(value, key = "") {
  if (SENSITIVE_KEY.test(key)) return "[redacted]";
  if (Array.isArray(value)) return value.slice(0, 100).map((item) => redact(item, key));
  if (value && typeof value === "object") {
    const output = {};
    for (const [childKey, childValue] of Object.entries(value).slice(0, 100)) output[String(childKey).slice(0, 80)] = redact(childValue, String(childKey));
    return output;
  }
  return value;
}

function boundedJSON(value, maximum = 16000) {
  try {
    const encoded = JSON.stringify(redact(value));
    return encoded.length <= maximum ? encoded : JSON.stringify({ truncated: true });
  } catch {
    return "{}";
  }
}

function jobView(row) {
  if (!row) return null;
  return {
    id: row.id,
    status: row.status,
    stage: row.stage,
    inputKey: row.input_key,
    inputName: row.input_name,
    config: (() => {
      try { return JSON.parse(row.config_json || "{}"); } catch { return {}; }
    })(),
    totalSources: Number(row.total_sources || 0),
    duplicateCount: Number(row.duplicate_count || 0),
    completedCount: Number(row.completed_count || 0),
    passedCount: Number(row.passed_count || 0),
    progress: Number(row.progress || 0),
    error: row.error || "",
    cancelRequested: Boolean(row.cancel_requested),
    attemptCount: Number(row.attempt_count || 0),
    resultCount: Number(row.result_count || 0),
    createdAt: Number(row.created_at || 0),
    updatedAt: Number(row.updated_at || 0),
  };
}

async function getJob(env, jobId) {
  return env.DB.prepare("SELECT * FROM jobs WHERE id = ?1").bind(jobId).first();
}

async function createJob(request, env) {
  let payload;
  try {
    payload = await readJSON(request, maxInt(env.MAX_INPUT_BYTES, 33554432, 1024, 67108864));
  } catch (error) {
    return textError(request, env, 413, error instanceof Error ? error.message : String(error));
  }
  if (!payload || typeof payload !== "object") return textError(request, env, 400, "job payload must be an object");
  let inputKey = safeInputKey(payload.inputKey);
  let inputName = String(payload.inputName || "upload.json").slice(0, 255);
  if (Array.isArray(payload.sources)) {
    if (payload.sources.length > 20000) return textError(request, env, 413, "source limit exceeded (20000)");
    const serialized = JSON.stringify(payload.sources);
    const bytes = new TextEncoder().encode(serialized);
    const maximum = maxInt(env.MAX_INPUT_BYTES, 33554432, 1024, 67108864);
    if (bytes.byteLength > maximum) return textError(request, env, 413, `payload exceeds ${maximum} bytes`);
    inputKey = `inputs/${crypto.randomUUID()}.json`;
    await env.INPUTS.put(inputKey, bytes, { httpMetadata: { contentType: "application/json; charset=utf-8" } });
  }
  if (!inputKey) return textError(request, env, 400, "inputKey or sources is required");
  const jobId = crypto.randomUUID();
  const config = normalizeConfig(payload.config);
  const timestamp = now();
  await env.DB.prepare(
    `INSERT INTO jobs (id, status, stage, input_key, input_name, config_json, total_sources, created_at, updated_at)
     VALUES (?1, 'queued', 'queued', ?2, ?3, ?4, ?5, ?6, ?6)`,
  ).bind(jobId, inputKey, inputName, JSON.stringify(config), maxInt(payload.sourceCount, 0, 0, 20000), timestamp).run();
  await env.DB.prepare("INSERT INTO job_events (job_id, level, stage, message, payload_json, created_at) VALUES (?1,'info','queued','job queued for AMD executor','{}',?2)")
    .bind(jobId, timestamp).run();
  return json(request, env, { job: jobView(await getJob(env, jobId)), deduplicated: 0 }, 201);
}

async function uploadInput(request, env) {
  const maximum = maxInt(env.MAX_INPUT_BYTES, 33554432, 1024, 67108864);
  let bytes;
  try { bytes = await readBytes(request, maximum); } catch (error) {
    return textError(request, env, 413, error instanceof Error ? error.message : String(error));
  }
  if (!bytes.byteLength) return textError(request, env, 400, "empty upload");
  const filename = String(request.headers.get("x-input-name") || "upload.json").replace(/[^a-zA-Z0-9._-]+/g, "_").slice(0, 100) || "upload.json";
  const inputKey = `inputs/${crypto.randomUUID()}-${filename}`;
  await env.INPUTS.put(inputKey, bytes, { httpMetadata: { contentType: request.headers.get("content-type") || "application/json" } });
  return json(request, env, { inputKey, inputName: filename, bytes: bytes.byteLength }, 201);
}

async function getPublicJob(request, env, jobId) {
  const row = await getJob(env, jobId);
  return row ? json(request, env, jobView(row)) : textError(request, env, 404, "job not found");
}

function stageWhere(stage) {
  if (stage === "quick") return "quick_status='passed'";
  if (stage === "full") return "full_status='passed'";
  if (["passed", "failed", "pending"].includes(stage)) return `final_status='${stage}'`;
  return "1=1";
}

async function getPublicSources(request, env, jobId, url) {
  if (!await getJob(env, jobId)) return textError(request, env, 404, "job not found");
  const offset = maxInt(url.searchParams.get("offset"), 0, 0, 200000);
  const limit = maxInt(url.searchParams.get("limit"), 100, 1, 1000);
  const stage = url.searchParams.get("stage") || "";
  const rows = await env.DB.prepare(
    `SELECT source_key, source_name, source_url, duplicate_count, quick_status, full_status,
            stability_pass_count, final_status, last_error
       FROM job_sources WHERE job_id=?1 AND ${stageWhere(stage)}
       ORDER BY source_name, source_url LIMIT ?2 OFFSET ?3`,
  ).bind(jobId, limit, offset).all();
  const items = (rows.results || []).map((row) => ({
    sourceKey: row.source_key,
    sourceName: row.source_name,
    sourceUrl: row.source_url,
    duplicateCount: Number(row.duplicate_count || 0),
    quickStatus: row.quick_status,
    fullStatus: row.full_status,
    stabilityPassCount: Number(row.stability_pass_count || 0),
    finalStatus: row.final_status,
    lastError: row.last_error || "",
  }));
  return json(request, env, { items, offset, limit, stage });
}

async function getPublicEvents(request, env, jobId, url) {
  if (!await getJob(env, jobId)) return textError(request, env, 404, "job not found");
  const afterId = maxInt(url.searchParams.get("afterId"), 0, 0, 1000000000);
  const limit = maxInt(url.searchParams.get("limit"), 200, 1, 1000);
  const rows = await env.DB.prepare(
    "SELECT id, level, stage, message, payload_json, created_at FROM job_events WHERE job_id=?1 AND id>?2 ORDER BY id LIMIT ?3",
  ).bind(jobId, afterId, limit).all();
  const items = (rows.results || []).map((row) => ({
    id: Number(row.id), level: row.level, stage: row.stage, message: row.message,
    payload: (() => { try { return JSON.parse(row.payload_json || "{}"); } catch { return {}; } })(),
    createdAt: Number(row.created_at || 0),
  }));
  return json(request, env, { items });
}

async function getPublicResult(request, env, jobId) {
  const row = await getJob(env, jobId);
  if (!row) return textError(request, env, 404, "job not found");
  if (row.status !== "completed" || !row.result_key) return textError(request, env, 409, "job is not completed");
  const object = await env.RESULTS.get(row.result_key);
  if (!object) return textError(request, env, 404, "result artifact not found");
  return new Response(object.body, { status: 200, headers: corsHeaders(request, env, { "content-type": "application/json; charset=utf-8", etag: object.httpEtag || "" }) });
}

async function cancelJob(request, env, jobId) {
  const result = await env.DB.prepare(
    "UPDATE jobs SET cancel_requested=1, updated_at=?1 WHERE id=?2 AND status IN ('queued','running','resuming')",
  ).bind(now(), jobId).run();
  if (!result.meta || !result.meta.changes) return textError(request, env, 404, "job not found or already finished");
  await env.DB.prepare("INSERT INTO job_events (job_id, level, stage, message, payload_json, created_at) VALUES (?1,'info','cancelling','cancel requested','{}',?2)")
    .bind(jobId, now()).run();
  return json(request, env, jobView(await getJob(env, jobId)));
}

async function resumeJob(request, env, jobId) {
  const row = await getJob(env, jobId);
  if (!row) return textError(request, env, 404, "job not found");
  if (!['cancelled', 'failed'].includes(row.status)) return textError(request, env, 409, "job cannot be resumed in its current state");
  await env.DB.prepare("UPDATE jobs SET status='queued', stage='resuming', cancel_requested=0, error='', attempt_count=0, updated_at=?1 WHERE id=?2")
    .bind(now(), jobId).run();
  return json(request, env, jobView(await getJob(env, jobId)));
}

async function claimJob(request, env, jobId) {
  const executorId = String(request.headers.get("x-executor-id") || "").slice(0, 120);
  if (!executorId) return textError(request, env, 400, "x-executor-id is required");
  const leaseUntil = now() + maxInt(env.QUEUE_VISIBILITY_TIMEOUT_MS, 43200000, 60000, 43200000);
  const result = await env.DB.prepare(
    `UPDATE jobs SET status='running', stage=CASE WHEN stage IN ('queued','resuming') THEN 'claimed' ELSE stage END,
            executor_id=?1, lease_expires_at=?2, attempt_count=attempt_count+1, updated_at=?3
       WHERE id=?4 AND cancel_requested=0
         AND (status='queued' OR (status='running' AND (executor_id=?1 OR lease_expires_at<?3)))`,
  ).bind(executorId, leaseUntil, now(), jobId).run();
  if (!result.meta || !result.meta.changes) {
    const row = await getJob(env, jobId);
    if (!row) return textError(request, env, 404, "job not found");
    return textError(request, env, 409, `job is ${row.status}`);
  }
  return json(request, env, jobView(await getJob(env, jobId)));
}

async function claimNextJob(request, env) {
  const executorId = String(request.headers.get("x-executor-id") || "").slice(0, 120);
  if (!executorId) return textError(request, env, 400, "x-executor-id is required");
  const timestamp = now();
  const leaseUntil = timestamp + maxInt(env.QUEUE_VISIBILITY_TIMEOUT_MS, 43200000, 60000, 43200000);

  // A restarted executor must resume its own live lease before claiming a
  // second job. This also makes duplicate polling safe when two requests
  // overlap during a transient network retry.
  const active = await env.DB.prepare(
    "SELECT * FROM jobs WHERE executor_id=?1 AND status='running' AND lease_expires_at>?2 ORDER BY updated_at DESC LIMIT 1",
  ).bind(executorId, timestamp).first();
  if (active) return json(request, env, { job: jobView(active), claimed: false, resumed: true });

  // D1 executes this as one SQLite UPDATE. The subquery chooses the oldest
  // queued job, or an expired lease for crash recovery, while the outer WHERE
  // protects against a stale candidate being claimed by another executor.
  const result = await env.DB.prepare(
    `UPDATE jobs SET status='running',
            stage=CASE WHEN stage IN ('queued','resuming') THEN 'claimed' ELSE stage END,
            executor_id=?1, lease_expires_at=?2, attempt_count=attempt_count+1, updated_at=?3
       WHERE id=(
         SELECT id FROM jobs
          WHERE cancel_requested=0 AND (
            status IN ('queued','resuming') OR
            (status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at<?3)
          )
          ORDER BY CASE WHEN status='running' THEN 1 ELSE 0 END, created_at, id
          LIMIT 1
       )
         AND cancel_requested=0
         AND (
           status IN ('queued','resuming') OR
           (status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at<?3)
         )`,
  ).bind(executorId, leaseUntil, timestamp).run();
  if (!result.meta || !result.meta.changes) return json(request, env, { job: null, claimed: false });
  const job = await env.DB.prepare(
    "SELECT * FROM jobs WHERE executor_id=?1 AND lease_expires_at=?2 ORDER BY updated_at DESC LIMIT 1",
  ).bind(executorId, leaseUntil).first();
  return json(request, env, { job: jobView(job), claimed: Boolean(job), resumed: false });
}

async function internalJob(request, env, jobId) {
  const row = await getJob(env, jobId);
  return row ? json(request, env, jobView(row)) : textError(request, env, 404, "job not found");
}

async function internalInput(request, env, jobId) {
  const executorId = String(request.headers.get("x-executor-id") || "");
  const row = await getJob(env, jobId);
  if (!row) return textError(request, env, 404, "job not found");
  if (!executorId || row.executor_id !== executorId) return textError(request, env, 409, "job is not leased by this executor");
  const object = await env.INPUTS.get(row.input_key);
  if (!object) return textError(request, env, 404, "input artifact not found");
  return new Response(object.body, { status: 200, headers: corsHeaders(request, env, { "content-type": "application/json; charset=utf-8", etag: object.httpEtag || "" }) });
}

function sourceItem(item) {
  const source = item && typeof item === "object" ? item : {};
  return {
    sourceKey: String(source.sourceKey || "").slice(0, 512),
    sourceName: String(source.sourceName || "").slice(0, 300),
    sourceUrl: String(source.sourceUrl || "").slice(0, 2048),
    duplicateCount: maxInt(source.duplicateCount, 0, 0, 20000),
    quickStatus: String(source.quickStatus || "pending").slice(0, 32),
    fullStatus: String(source.fullStatus || "pending").slice(0, 32),
    stabilityPassCount: maxInt(source.stabilityPassCount, 0, 0, 5),
    finalStatus: String(source.finalStatus || "pending").slice(0, 32),
    lastError: String(source.lastError || "").slice(0, 500),
  };
}

function chunk(items, size) {
  const output = [];
  for (let index = 0; index < items.length; index += size) output.push(items.slice(index, index + size));
  return output;
}

async function internalProgress(request, env, jobId) {
  let payload;
  try { payload = await readJSON(request, maxInt(env.MAX_PROGRESS_BYTES, 1048576, 1024, 4194304)); } catch (error) {
    return textError(request, env, 413, error instanceof Error ? error.message : String(error));
  }
  const executorId = String(request.headers.get("x-executor-id") || "");
  const row = await getJob(env, jobId);
  if (!row) return textError(request, env, 404, "job not found");
  if (!executorId || row.executor_id !== executorId) return textError(request, env, 409, "job is not leased by this executor");
  const timestamp = now();
  const sourceItems = Array.isArray(payload.sources) ? payload.sources.slice(0, 300).map(sourceItem).filter((item) => item.sourceKey) : [];
  const eventItems = Array.isArray(payload.events) ? payload.events.slice(0, 100) : [];
  const statements = [];
  for (const item of sourceItems) {
    statements.push(env.DB.prepare(
      `INSERT INTO job_sources (job_id, source_key, source_name, source_url, duplicate_count, quick_status, full_status,
          stability_pass_count, final_status, last_error, updated_at)
       VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)
       ON CONFLICT(job_id, source_key) DO UPDATE SET source_name=excluded.source_name, source_url=excluded.source_url,
          duplicate_count=excluded.duplicate_count, quick_status=excluded.quick_status, full_status=excluded.full_status,
          stability_pass_count=excluded.stability_pass_count, final_status=excluded.final_status,
          last_error=excluded.last_error, updated_at=excluded.updated_at`,
    ).bind(jobId, item.sourceKey, item.sourceName, item.sourceUrl, item.duplicateCount, item.quickStatus, item.fullStatus,
      item.stabilityPassCount, item.finalStatus, item.lastError, timestamp));
  }
  for (const event of eventItems) {
    const level = String(event && event.level || "info").slice(0, 16);
    const stage = String(event && event.stage || payload.stage || "").slice(0, 64);
    const message = String(event && event.message || "").slice(0, 500);
    if (!message) continue;
    statements.push(env.DB.prepare(
      "INSERT INTO job_events (job_id, level, stage, message, payload_json, created_at) VALUES (?1,?2,?3,?4,?5,?6)",
    ).bind(jobId, level, stage, message, boundedJSON(event.payload || {}), timestamp));
  }
  for (const batch of chunk(statements, 40)) if (batch.length) await env.DB.batch(batch);
  const status = ["queued", "running", "cancelled"].includes(String(payload.status)) ? String(payload.status) : "running";
  const stage = String(payload.stage || row.stage || "running").slice(0, 64);
  const progress = maxFloat(payload.progress, Number(row.progress || 0), 0, 0.99);
  const totalSources = maxInt(payload.totalSources, Number(row.total_sources || 0), 0, 20000);
  const duplicateCount = maxInt(payload.duplicateCount, Number(row.duplicate_count || 0), 0, 20000);
  const completedCount = maxInt(payload.completedCount, Number(row.completed_count || 0), 0, 20000);
  const passedCount = maxInt(payload.passedCount, Number(row.passed_count || 0), 0, 20000);
  await env.DB.prepare(
    `UPDATE jobs SET status=?1, stage=?2, progress=?3, total_sources=?4, duplicate_count=?5,
            completed_count=?6, passed_count=?7, lease_expires_at=?8, updated_at=?9 WHERE id=?10 AND executor_id=?11`,
  ).bind(status, stage, progress, totalSources, duplicateCount, completedCount, passedCount,
    timestamp + maxInt(env.QUEUE_VISIBILITY_TIMEOUT_MS, 43200000, 60000, 43200000), timestamp, jobId, executorId).run();
  return json(request, env, jobView(await getJob(env, jobId)));
}

async function internalResult(request, env, jobId) {
  const executorId = String(request.headers.get("x-executor-id") || "");
  const row = await getJob(env, jobId);
  if (!row) return textError(request, env, 404, "job not found");
  if (!executorId || row.executor_id !== executorId) return textError(request, env, 409, "job is not leased by this executor");
  let bytes;
  try { bytes = await readBytes(request, maxInt(env.MAX_RESULT_BYTES, 33554432, 1024, 67108864)); } catch (error) {
    return textError(request, env, 413, error instanceof Error ? error.message : String(error));
  }
  if (!bytes.byteLength) return textError(request, env, 400, "empty result");
  const resultKey = `results/${jobId}.json`;
  await env.RESULTS.put(resultKey, bytes, { httpMetadata: { contentType: "application/json; charset=utf-8" } });
  const sourceCount = maxInt(request.headers.get("x-source-count"), 0, 0, 20000);
  const timestamp = now();
  await env.DB.prepare(
    `UPDATE jobs SET status='completed', stage='completed', progress=1, passed_count=?1, result_count=?1,
            result_key=?2, error='', cancel_requested=0, lease_expires_at=NULL, updated_at=?3 WHERE id=?4 AND executor_id=?5`,
  ).bind(sourceCount, resultKey, timestamp, jobId, executorId).run();
  await env.DB.prepare("INSERT INTO job_events (job_id, level, stage, message, payload_json, created_at) VALUES (?1,'info','completed','validation completed',?2,?3)")
    .bind(jobId, JSON.stringify({ passed: sourceCount }), timestamp).run();
  return json(request, env, jobView(await getJob(env, jobId)));
}

async function internalFail(request, env, jobId) {
  let payload = {};
  try { payload = await readJSON(request, 65536); } catch { /* use defaults */ }
  const executorId = String(request.headers.get("x-executor-id") || "");
  const row = await getJob(env, jobId);
  if (!row) return textError(request, env, 404, "job not found");
  if (!executorId || row.executor_id !== executorId) return textError(request, env, 409, "job is not leased by this executor");
  const retryable = payload.retryable !== false;
  const status = retryable ? "queued" : "failed";
  const message = String(payload.error || "executor failed").slice(0, 500);
  const timestamp = now();
  const result = await env.DB.prepare("UPDATE jobs SET status=?1, stage=?2, error=?3, executor_id=NULL, lease_expires_at=NULL, updated_at=?4 WHERE id=?5 AND executor_id=?6")
    .bind(status, retryable ? "retrying" : "failed", message, timestamp, jobId, executorId).run();
  if (!result.meta || !result.meta.changes) return textError(request, env, 409, "job lease changed");
  await env.DB.prepare("INSERT INTO job_events (job_id, level, stage, message, payload_json, created_at) VALUES (?1,'error',?2,?3,?4,?5)")
    .bind(jobId, retryable ? "retrying" : "failed", message, boundedJSON({ retryable }), timestamp).run();
  return json(request, env, jobView(await getJob(env, jobId)));
}

async function internalCancelled(request, env, jobId) {
  const executorId = String(request.headers.get("x-executor-id") || "");
  const row = await getJob(env, jobId);
  if (!row) return textError(request, env, 404, "job not found");
  if (!executorId || row.executor_id !== executorId) return textError(request, env, 409, "job is not leased by this executor");
  const result = await env.DB.prepare("UPDATE jobs SET status='cancelled', stage='cancelled', executor_id=NULL, lease_expires_at=NULL, updated_at=?1 WHERE id=?2 AND executor_id=?3")
    .bind(now(), jobId, executorId).run();
  if (!result.meta || !result.meta.changes) return textError(request, env, 409, "job lease changed");
  return json(request, env, jobView(await getJob(env, jobId)));
}

async function routeAPI(request, env, url) {
  const segments = url.pathname.split("/").filter(Boolean);
  if (segments[0] !== "api") return null;
  if (segments[1] === "healthz" && request.method === "GET") return json(request, env, { ok: true, service: "readori-source-validator-control", time: now() });
  const auth = publicAuth(request, env);
  if (auth.response) return auth.response;
  if (segments[1] === "uploads" && request.method === "POST") return uploadInput(request, env);
  if (segments[1] === "jobs" && segments.length === 2 && request.method === "POST") return createJob(request, env);
  if (segments[1] !== "jobs" || !segments[2]) return textError(request, env, 404, "route not found");
  const jobId = segments[2];
  if (segments.length === 3 && request.method === "GET") return getPublicJob(request, env, jobId);
  if (segments[3] === "sources" && request.method === "GET") return getPublicSources(request, env, jobId, url);
  if (segments[3] === "events" && request.method === "GET") return getPublicEvents(request, env, jobId, url);
  if (segments[3] === "result" && request.method === "GET") return getPublicResult(request, env, jobId);
  if (segments[3] === "cancel" && request.method === "POST") return cancelJob(request, env, jobId);
  if (segments[3] === "resume" && request.method === "POST") return resumeJob(request, env, jobId);
  return textError(request, env, 404, "route not found");
}

async function routeInternal(request, env, url) {
  const auth = executorAuth(request, env);
  if (auth.response) return auth.response;
  const segments = url.pathname.split("/").filter(Boolean);
  if (segments[0] !== "internal") return textError(request, env, 404, "route not found");
  if (segments[1] === "next" && segments.length === 2 && request.method === "POST") return claimNextJob(request, env);
  if (segments[1] !== "jobs" || !segments[2]) return textError(request, env, 404, "route not found");
  const jobId = segments[2];
  if (segments.length === 3 && request.method === "GET") return internalJob(request, env, jobId);
  if (segments[3] === "claim" && request.method === "POST") return claimJob(request, env, jobId);
  if (segments[3] === "input" && request.method === "GET") return internalInput(request, env, jobId);
  if (segments[3] === "progress" && request.method === "POST") return internalProgress(request, env, jobId);
  if (segments[3] === "result" && request.method === "POST") return internalResult(request, env, jobId);
  if (segments[3] === "fail" && request.method === "POST") return internalFail(request, env, jobId);
  if (segments[3] === "cancelled" && request.method === "POST") return internalCancelled(request, env, jobId);
  return textError(request, env, 404, "route not found");
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(request, env) });
    const url = new URL(request.url);
    try {
      if (url.pathname.startsWith("/api/")) return await routeAPI(request, env, url);
      if (url.pathname.startsWith("/internal/")) return await routeInternal(request, env, url);
      if (env.ASSETS) return env.ASSETS.fetch(request);
      return textError(request, env, 404, "not found");
    } catch (error) {
      console.error("control-plane request failed", error instanceof Error ? error.message : String(error));
      return textError(request, env, 500, "control-plane request failed");
    }
  },
};
