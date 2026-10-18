import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";

const ACCESS = "access-secret-for-tests";
const env = {
  VALIDATOR_SERVER_URL: "https://validator.example",
  VALIDATOR_SERVER_TOKEN: "server-secret",
  READORI_VALIDATOR_ACCESS_TOKEN: ACCESS,
  FRONTEND_ORIGIN: "https://syyz.readori.com",
};

// A request that carries the console access token, like the browser console does.
function authed(url, init = {}) {
  const headers = new Headers(init.headers || {});
  headers.set("authorization", `Bearer ${ACCESS}`);
  return new Request(url, { ...init, headers });
}

test("preflight does not require D1 or a browser API key", async () => {
  const response = await worker.fetch(new Request("https://control.example/api/jobs", { method: "OPTIONS" }), {});
  assert.equal(response.status, 204);
  assert.match(response.headers.get("access-control-allow-methods"), /POST/);
});

test("preflight allows the configured public custom domain", async () => {
  const response = await worker.fetch(new Request("https://syyz.readori.com/api/jobs", {
    method: "OPTIONS",
    headers: { Origin: "https://syyz.readori.com" },
  }), env);
  assert.equal(response.status, 204);
  assert.equal(response.headers.get("access-control-allow-origin"), "https://syyz.readori.com");
});

test("missing server configuration fails closed without touching D1", async () => {
  const response = await worker.fetch(new Request("https://control.example/api/healthz"), {});
  assert.equal(response.status, 503);
  assert.equal((await response.json()).error, "VALIDATOR_SERVER_URL is not configured");
});

test("health is proxied to the server without an access token and without the backend token", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async (request, options) => {
    assert.equal(new URL(request).pathname, "/healthz");
    assert.equal(options.headers.get("x-api-key"), null);
    assert.equal(options.headers.get("authorization"), null);
    return new Response(JSON.stringify({ ok: true, service: "readori-source-validator" }), { status: 200, headers: { "content-type": "application/json" } });
  };
  try {
    const response = await worker.fetch(new Request("https://control.example/api/healthz"), env);
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { ok: true, service: "readori-source-validator" });
  } finally {
    globalThis.fetch = original;
  }
});

test("job creation is forwarded with the server token and compatible field names", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async (request, options) => {
    assert.equal(new URL(request).pathname, "/v1/jobs");
    assert.equal(options.headers.get("x-api-key"), "server-secret");
    const body = JSON.parse(options.body);
    assert.equal(body.input_name, "sources.json");
    assert.equal("inputName" in body, false);
    return new Response(JSON.stringify({ job: { id: "job-1", status: "queued", input_name: "sources.json", source_count: 2, config_json: "{}" }, deduplicated: 1 }), { status: 201, headers: { "content-type": "application/json" } });
  };
  try {
    const response = await worker.fetch(authed("https://control.example/api/jobs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ inputName: "sources.json", sources: [{ bookSourceUrl: "https://example.com" }], config: {} }),
    }), env);
    assert.equal(response.status, 201);
    const body = await response.json();
    assert.equal(body.job.inputName, "sources.json");
    assert.equal(body.job.totalSources, 2);
    assert.equal(body.deduplicated, 1);
  } finally {
    globalThis.fetch = original;
  }
});

test("job creation passes config.app_version through to the server", async () => {
  const original = globalThis.fetch;
  let forwarded = null;
  globalThis.fetch = async (request, options) => {
    assert.equal(new URL(request).pathname, "/v1/jobs");
    forwarded = JSON.parse(options.body);
    return new Response(JSON.stringify({ job: { id: "job-2", status: "queued", input_name: "sources.json", source_count: 1, config_json: JSON.stringify({ app_version: "1.1.2" }) }, deduplicated: 0 }), { status: 201, headers: { "content-type": "application/json" } });
  };
  try {
    const response = await worker.fetch(authed("https://control.example/api/jobs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ inputName: "sources.json", sources: [{ bookSourceUrl: "https://example.com" }], config: { app_version: "1.1.2" } }),
    }), env);
    assert.equal(response.status, 201);
    assert.equal(forwarded.config.app_version, "1.1.2");
    const body = await response.json();
    assert.equal(body.job.config.app_version, "1.1.2");
  } finally {
    globalThis.fetch = original;
  }
});

test("status and event polling are translated from the local server API", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async (request) => {
    const url = new URL(request);
    if (url.pathname === "/v1/jobs/job-1") {
      return new Response(JSON.stringify({ id: "job-1", status: "running", stage: "quick-scan", source_count: 2, completed_count: 1, passed_count: 1, config_json: "{}" }), { headers: { "content-type": "application/json" } });
    }
    assert.equal(url.pathname, "/v1/jobs/job-1/events");
    assert.equal(url.searchParams.get("after_id"), "7");
    return new Response(JSON.stringify({ items: [{ id: 8, stage: "quick-scan", message: "ok", created_at: "now", payload_json: "{}" }] }), { headers: { "content-type": "application/json" } });
  };
  try {
    const status = await worker.fetch(authed("https://control.example/api/jobs/job-1"), env);
    const job = await status.json();
    assert.equal(job.completedCount, 1);
    assert.equal(job.totalSources, 2);
    const events = await worker.fetch(authed("https://control.example/api/jobs/job-1/events?afterId=7"), env);
    const body = await events.json();
    assert.equal(body.items[0].createdAt, "now");
    assert.deepEqual(body.items[0].payload, {});
  } finally {
    globalThis.fetch = original;
  }
});

test("upstream failures are returned without D1 fallback", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "server unavailable" }), { status: 503, headers: { "content-type": "application/json" } });
  try {
    const response = await worker.fetch(authed("https://control.example/api/jobs/job-1"), env);
    assert.equal(response.status, 503);
    assert.equal((await response.json()).detail, "server unavailable");
  } finally {
    globalThis.fetch = original;
  }
});

// --- access control (R145a) -------------------------------------------------------------------------------

function trapFetch() {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (request, options) => {
    calls.push({ url: String(request), options });
    return new Response(JSON.stringify({ job: { id: "job-1", status: "queued" } }), { status: 200, headers: { "content-type": "application/json" } });
  };
  return { calls, restore: () => { globalThis.fetch = original; } };
}

const sampleJobBody = JSON.stringify({ inputName: "sources.json", sources: [{ bookSourceUrl: "https://example.com" }], config: {} });

test("requests without an access token are refused and never reach the backend", async () => {
  const trap = trapFetch();
  try {
    const submit = await worker.fetch(new Request("https://control.example/api/jobs", { method: "POST", headers: { "content-type": "application/json" }, body: sampleJobBody }), env);
    assert.equal(submit.status, 401);
    assert.match((await submit.json()).error, /访问令牌/);
    assert.match(submit.headers.get("www-authenticate"), /^Bearer/);
    for (const [path, method] of [
      ["/api/jobs/job-1", "GET"],
      ["/api/jobs/job-1/events", "GET"],
      ["/api/jobs/job-1/sources", "GET"],
      ["/api/jobs/job-1/result", "GET"],
      ["/api/jobs/job-1/cancel", "POST"],
      ["/api/jobs/job-1/resume", "POST"],
      ["/api/uploads", "POST"],
    ]) {
      const response = await worker.fetch(new Request(`https://control.example${path}`, { method }), env);
      assert.equal(response.status, 401, `${method} ${path}`);
    }
    assert.equal(trap.calls.length, 0, "the backend must not be called for anonymous requests");
  } finally {
    trap.restore();
  }
});

test("a wrong access token is refused", async () => {
  const trap = trapFetch();
  try {
    for (const header of ["Bearer wrong-token", `Bearer ${ACCESS}x`, `Bearer ${ACCESS.slice(0, -1)}`, ACCESS, "Basic abc", "Bearer", "Bearer "]) {
      const response = await worker.fetch(new Request("https://control.example/api/jobs/job-1/cancel", { method: "POST", headers: { authorization: header } }), env);
      assert.equal(response.status, 401, header);
    }
    // The backend token is not an access token either.
    const leaked = await worker.fetch(new Request("https://control.example/api/jobs/job-1", { headers: { "x-api-key": "server-secret", authorization: "Bearer server-secret" } }), env);
    assert.equal(leaked.status, 401);
    assert.equal(trap.calls.length, 0);
  } finally {
    trap.restore();
  }
});

test("missing access-token secret fails closed with 503 on every protected route", async () => {
  const trap = trapFetch();
  try {
    const open = { ...env };
    delete open.READORI_VALIDATOR_ACCESS_TOKEN;
    for (const configured of [open, { ...env, READORI_VALIDATOR_ACCESS_TOKEN: "   " }]) {
      // Even a caller that sends *some* bearer token (or an empty one) must not get through.
      for (const headers of [{}, { authorization: "Bearer " }, { authorization: "Bearer anything" }, { authorization: `Bearer ${ACCESS}` }]) {
        const response = await worker.fetch(new Request("https://control.example/api/jobs", { method: "POST", headers: { "content-type": "application/json", ...headers }, body: sampleJobBody }), configured);
        assert.equal(response.status, 503);
        assert.match((await response.json()).error, /未配置访问令牌/);
      }
    }
    assert.equal(trap.calls.length, 0);
    // Health stays reachable so operators can still probe the deployment.
    const health = await worker.fetch(new Request("https://control.example/api/healthz"), open);
    assert.equal(health.status, 200);
  } finally {
    trap.restore();
  }
});

test("a correct access token is forwarded with the backend token injected and the user token stripped", async () => {
  const trap = trapFetch();
  try {
    const response = await worker.fetch(authed("https://control.example/api/jobs", {
      method: "POST",
      headers: { "content-type": "application/json", cookie: "session=1" },
      body: sampleJobBody,
    }), env);
    assert.equal(response.status, 200);
    assert.equal(trap.calls.length, 1);
    const { url, options } = trap.calls[0];
    assert.equal(new URL(url).origin, "https://validator.example");
    assert.equal(new URL(url).pathname, "/v1/jobs");
    assert.equal(options.headers.get("x-api-key"), "server-secret");
    assert.equal(options.headers.get("authorization"), null, "the caller's access token must not reach the backend");
    assert.equal(options.headers.get("cookie"), null);
  } finally {
    trap.restore();
  }
});

test("a caller-supplied x-api-key never replaces the backend token", async () => {
  const trap = trapFetch();
  try {
    await worker.fetch(authed("https://control.example/api/jobs/job-1", { headers: { "x-api-key": "attacker-chosen" } }), env);
    assert.equal(trap.calls[0].options.headers.get("x-api-key"), "server-secret");
  } finally {
    trap.restore();
  }
});

test("the access token also guards cancel, resume, result and sources", async () => {
  const trap = trapFetch();
  try {
    for (const [path, method, upstream] of [
      ["/api/jobs/job-1/cancel", "POST", "/v1/jobs/job-1/cancel"],
      ["/api/jobs/job-1/resume", "POST", "/v1/jobs/job-1/resume"],
      ["/api/jobs/job-1/result", "GET", "/v1/jobs/job-1/result"],
      ["/api/jobs/job-1/sources", "GET", "/v1/jobs/job-1/sources"],
    ]) {
      trap.calls.length = 0;
      const response = await worker.fetch(authed(`https://control.example${path}`, { method }), env);
      assert.equal(response.status, 200, `${method} ${path}`);
      assert.equal(new URL(trap.calls[0].url).pathname, upstream);
      assert.equal(trap.calls[0].options.headers.get("x-api-key"), "server-secret");
    }
  } finally {
    trap.restore();
  }
});

test("health check needs no access token", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "content-type": "application/json" } });
  try {
    const response = await worker.fetch(new Request("https://control.example/api/healthz"), env);
    assert.equal(response.status, 200);
    // A wrong token on the health route is simply ignored, not an error.
    const withWrong = await worker.fetch(new Request("https://control.example/api/healthz", { headers: { authorization: "Bearer nope" } }), env);
    assert.equal(withWrong.status, 200);
  } finally {
    globalThis.fetch = original;
  }
});

test("unknown routes and wrong methods are rejected before reaching the backend", async () => {
  const trap = trapFetch();
  try {
    assert.equal((await worker.fetch(authed("https://control.example/api/jobs/job-1/anything"), env)).status, 404);
    assert.equal((await worker.fetch(authed("https://control.example/api/admin"), env)).status, 404);
    assert.equal((await worker.fetch(authed("https://control.example/api/jobs/job-1/cancel"), env)).status, 405);
    assert.equal((await worker.fetch(authed("https://control.example/api/jobs", { method: "GET" }), env)).status, 405);
    assert.equal((await worker.fetch(authed("https://control.example/api/jobs/job-1", { method: "POST" }), env)).status, 405);
    assert.equal(trap.calls.length, 0);
  } finally {
    trap.restore();
  }
});

test("input_path is stripped from job creation so callers cannot read server files", async () => {
  const trap = trapFetch();
  try {
    await worker.fetch(authed("https://control.example/api/jobs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ input_path: "/etc/passwd", inputPath: "/etc/shadow", sources: [{ bookSourceUrl: "https://example.com" }] }),
    }), env);
    const forwarded = JSON.parse(trap.calls[0].options.body);
    assert.equal("input_path" in forwarded, false);
    assert.equal("inputPath" in forwarded, false);
    assert.equal(forwarded.sources.length, 1);
  } finally {
    trap.restore();
  }
});
