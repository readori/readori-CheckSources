const JSON_HEADERS = { "content-type": "application/json; charset=utf-8" };
const MAX_PROXY_BODY_BYTES = 64 * 1024 * 1024;

function corsHeaders(request, env, extra = {}) {
  const configured = String(env.FRONTEND_ORIGIN || "").trim();
  const requestOrigin = request.headers.get("Origin") || "";
  const origin = configured || requestOrigin || "*";
  return {
    ...JSON_HEADERS,
    "access-control-allow-origin": origin,
    "access-control-allow-headers": "authorization, content-type, x-api-key",
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

function serverConfig(env) {
  const raw = String(env.VALIDATOR_SERVER_URL || "").trim();
  if (!raw) return { error: "VALIDATOR_SERVER_URL is not configured" };
  let base;
  try {
    base = new URL(raw);
  } catch {
    return { error: "VALIDATOR_SERVER_URL is invalid" };
  }
  if (base.protocol !== "https:") return { error: "VALIDATOR_SERVER_URL must use HTTPS" };
  base.pathname = base.pathname.replace(/\/+$/, "");
  return { base, token: String(env.VALIDATOR_SERVER_TOKEN || "").trim() };
}

function publicJob(row) {
  if (!row || typeof row !== "object") return row;
  const config = row.config && typeof row.config === "object"
    ? row.config
    : (() => { try { return JSON.parse(row.config_json || "{}"); } catch { return {}; } })();
  return {
    id: row.id,
    status: row.status,
    stage: row.stage,
    inputKey: row.inputKey ?? row.input_key ?? "",
    inputName: row.inputName ?? row.input_name ?? "",
    config,
    totalSources: Number(row.totalSources ?? row.total_sources ?? row.source_count ?? 0),
    duplicateCount: Number(row.duplicateCount ?? row.duplicate_count ?? 0),
    completedCount: Number(row.completedCount ?? row.completed_count ?? 0),
    passedCount: Number(row.passedCount ?? row.passed_count ?? 0),
    progress: Number(row.progress || 0),
    error: row.error || "",
    cancelRequested: Boolean(row.cancelRequested ?? row.cancel_requested),
    attemptCount: Number(row.attemptCount ?? row.attempt_count ?? 0),
    resultCount: Number(row.resultCount ?? row.result_count ?? 0),
    createdAt: row.createdAt ?? row.created_at ?? 0,
    updatedAt: row.updatedAt ?? row.updated_at ?? 0,
  };
}

function transformPayload(path, payload) {
  if (!payload || typeof payload !== "object") return payload;
  if (path === "/v1/jobs" && payload.job) return { ...payload, job: publicJob(payload.job) };
  if (/^\/v1\/jobs\/[^/]+$/.test(path)) return publicJob(payload);
  if (/\/events$/.test(path) && Array.isArray(payload.items)) {
    return {
      ...payload,
      items: payload.items.map((event) => ({
        ...event,
        createdAt: event.createdAt ?? event.created_at ?? 0,
        payload: event.payload ?? (() => { try { return JSON.parse(event.payload_json || "{}"); } catch { return {}; } })(),
      })),
    };
  }
  if (/\/sources$/.test(path) && Array.isArray(payload.items)) {
    return {
      ...payload,
      items: payload.items.map((source) => ({
        ...source,
        sourceKey: source.sourceKey ?? source.source_key ?? "",
        sourceName: source.sourceName ?? source.source_name ?? "",
        sourceUrl: source.sourceUrl ?? source.source_url ?? "",
        duplicateCount: Number(source.duplicateCount ?? source.duplicate_count ?? 0),
        quickStatus: source.quickStatus ?? source.quick_status ?? "pending",
        fullStatus: source.fullStatus ?? source.full_status ?? "pending",
        stabilityPassCount: Number(source.stabilityPassCount ?? source.stability_pass_count ?? 0),
        finalStatus: source.finalStatus ?? source.final_status ?? "pending",
        lastError: source.lastError ?? source.last_error ?? "",
      })),
    };
  }
  return payload;
}

function upstreamPath(request) {
  const url = new URL(request.url);
  const parts = url.pathname.split("/").filter(Boolean);
  if (parts[0] !== "api") return null;
  if (parts[1] === "healthz" && parts.length === 2 && request.method === "GET") {
    return { path: "/healthz", query: "" };
  }
  if (parts[1] === "uploads" && parts.length === 2 && request.method === "POST") {
    return { path: "/v1/jobs/upload", query: url.search };
  }
  if (parts[1] !== "jobs" || !parts[2]) {
    if (parts[1] === "jobs" && parts.length === 2 && request.method === "POST") return { path: "/v1/jobs", query: url.search };
    return null;
  }
  const jobId = encodeURIComponent(parts[2]);
  const suffix = parts.slice(3);
  const suffixText = suffix.length ? `/${suffix.map((part) => encodeURIComponent(part)).join("/")}` : "";
  const target = `/v1/jobs/${jobId}${suffixText}`;
  const query = new URLSearchParams(url.search);
  if (query.has("afterId")) {
    query.set("after_id", query.get("afterId") || "0");
    query.delete("afterId");
  }
  return { path: target, query: query.toString() ? `?${query.toString()}` : "" };
}

async function proxy(request, env, target) {
  const config = serverConfig(env);
  if (config.error) return textError(request, env, 503, config.error);
  if (!config.token) return textError(request, env, 503, "VALIDATOR_SERVER_TOKEN is not configured");

  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("cookie");
  headers.set("x-api-key", config.token);
  headers.set("accept", "application/json");

  let body = request.body;
  if (request.method === "POST" && request.headers.get("content-type")?.includes("application/json")) {
    const declared = Number(request.headers.get("content-length") || 0);
    if (declared > MAX_PROXY_BODY_BYTES) return textError(request, env, 413, "payload too large");
    try {
      const payload = await request.clone().json();
      if (target.path === "/v1/jobs" && payload && typeof payload === "object" && payload.inputName && !payload.input_name) {
        payload.input_name = payload.inputName;
        delete payload.inputName;
      }
      body = JSON.stringify(payload);
      headers.set("content-type", "application/json");
      headers.delete("content-length");
    } catch {
      return textError(request, env, 400, "invalid JSON");
    }
  }

  const upstreamURL = new URL(`${config.base.toString().replace(/\/$/, "")}${target.path}${target.query}`);
  let response;
  try {
    response = await fetch(upstreamURL, { method: request.method, headers, body, redirect: "manual" });
  } catch (error) {
    console.error("validator server proxy failed", error instanceof Error ? error.message : String(error));
    return textError(request, env, 502, "validator server unavailable");
  }

  const contentType = response.headers.get("content-type") || "application/json; charset=utf-8";
  if (!contentType.includes("json")) {
    return new Response(response.body, { status: response.status, headers: corsHeaders(request, env, { "content-type": contentType }) });
  }
  let payload;
  try { payload = await response.json(); } catch { payload = { error: "invalid response from validator server" }; }
  const output = response.ok ? transformPayload(target.path, payload) : payload;
  return new Response(JSON.stringify(output), { status: response.status, headers: corsHeaders(request, env) });
}

async function routeAPI(request, env) {
  const target = upstreamPath(request);
  return target ? proxy(request, env, target) : textError(request, env, 404, "route not found");
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(request, env) });
    const url = new URL(request.url);
    try {
      if (url.pathname.startsWith("/api/")) return await routeAPI(request, env);
      if (env.ASSETS) return env.ASSETS.fetch(request);
      return textError(request, env, 404, "not found");
    } catch (error) {
      console.error("control-plane request failed", error instanceof Error ? error.message : String(error));
      return textError(request, env, 500, "control-plane request failed");
    }
  },
};
