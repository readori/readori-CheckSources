import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";

test("health endpoint is public and returns a stable service marker", async () => {
  const response = await worker.fetch(new Request("https://validator.example/api/healthz"), {});
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.ok, true);
  assert.equal(body.service, "readori-source-validator-control");
});

test("preflight does not require an API key", async () => {
  const response = await worker.fetch(new Request("https://validator.example/api/jobs", { method: "OPTIONS" }), {});
  assert.equal(response.status, 204);
  assert.match(response.headers.get("access-control-allow-methods"), /POST/);
});

test("public control plane accepts browser requests without an API key", async () => {
  const response = await worker.fetch(new Request("https://validator.example/api/jobs", { method: "GET" }), { PUBLIC_CONTROL_PLANE: "true" });
  assert.equal(response.status, 404);
  assert.equal((await response.json()).error, "route not found");
});

test("private control plane still requires an API key when public mode is disabled", async () => {
  const response = await worker.fetch(new Request("https://validator.example/api/jobs", { method: "GET" }), { PUBLIC_CONTROL_PLANE: "false" });
  assert.equal(response.status, 503);
  assert.equal((await response.json()).error, "CONTROL_API_KEY is not configured");
});

test("executor lease endpoint fails closed without the executor secret", async () => {
  const response = await worker.fetch(new Request("https://validator.example/internal/next", { method: "POST" }), {});
  assert.equal(response.status, 503);
  assert.equal((await response.json()).error, "EXECUTOR_TOKEN is not configured");
});

test("executor lease endpoint atomically returns a queued job", async () => {
  const row = {
    id: "job-1",
    status: "running",
    stage: "claimed",
    input_key: "inputs/job-1.json",
    input_name: "sources.json",
    config_json: "{}",
    total_sources: 2,
    duplicate_count: 0,
    completed_count: 0,
    passed_count: 0,
    progress: 0,
    error: "",
    cancel_requested: 0,
    result_count: 0,
    attempt_count: 1,
    created_at: 1,
    updated_at: 2,
  };
  const db = {
    prepare(sql) {
      return {
        bind(...args) {
          return {
            async first() {
              if (sql.includes("status='running' AND lease_expires_at>?2")) return null;
              return row;
            },
            async run() {
              return { meta: { changes: sql.startsWith("UPDATE jobs SET status='running'") ? 1 : 0 } };
            },
          };
        },
      };
    },
  };
  const response = await worker.fetch(
    new Request("https://validator.example/internal/next", {
      method: "POST",
      headers: { authorization: "Bearer executor-secret", "x-executor-id": "amd-1" },
      body: "{}",
    }),
    { EXECUTOR_TOKEN: "executor-secret", DB: db },
  );
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.claimed, true);
  assert.equal(body.job.id, "job-1");
  assert.equal(body.job.attemptCount, 1);
});
