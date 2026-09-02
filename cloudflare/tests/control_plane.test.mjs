import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";

const env = { VALIDATOR_SERVER_URL: "https://validator.example", VALIDATOR_SERVER_TOKEN: "server-secret" };

test("preflight does not require D1 or a browser API key", async () => {
  const response = await worker.fetch(new Request("https://control.example/api/jobs", { method: "OPTIONS" }), {});
  assert.equal(response.status, 204);
  assert.match(response.headers.get("access-control-allow-methods"), /POST/);
});

test("missing server configuration fails closed without touching D1", async () => {
  const response = await worker.fetch(new Request("https://control.example/api/healthz"), {});
  assert.equal(response.status, 503);
  assert.equal((await response.json()).error, "VALIDATOR_SERVER_URL is not configured");
});

test("health is proxied to the server", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async (request, options) => {
    assert.equal(new URL(request).pathname, "/healthz");
    assert.equal(options.headers.get("x-api-key"), "server-secret");
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
    const response = await worker.fetch(new Request("https://control.example/api/jobs", {
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
    const status = await worker.fetch(new Request("https://control.example/api/jobs/job-1"), env);
    const job = await status.json();
    assert.equal(job.completedCount, 1);
    assert.equal(job.totalSources, 2);
    const events = await worker.fetch(new Request("https://control.example/api/jobs/job-1/events?afterId=7"), env);
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
    const response = await worker.fetch(new Request("https://control.example/api/jobs/job-1"), env);
    assert.equal(response.status, 503);
    assert.equal((await response.json()).detail, "server unavailable");
  } finally {
    globalThis.fetch = original;
  }
});
