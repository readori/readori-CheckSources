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

test("public API fails closed when the control key is not configured", async () => {
  const response = await worker.fetch(new Request("https://validator.example/api/jobs", { method: "GET" }), {});
  assert.equal(response.status, 503);
  assert.equal((await response.json()).error, "CONTROL_API_KEY is not configured");
});
