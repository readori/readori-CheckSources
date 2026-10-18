import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";

const ACCESS = "access-secret-for-tests";
const SESSION = "Abcdefgh12345678";
const env = {
  VALIDATOR_SERVER_URL: "https://validator.example",
  VALIDATOR_SERVER_TOKEN: "server-secret",
  READORI_VALIDATOR_ACCESS_TOKEN: ACCESS,
  FRONTEND_ORIGIN: "https://syyz.readori.com",
};
const BASE = "https://control.example";

function authed(path, init = {}) {
  const headers = new Headers(init.headers || {});
  headers.set("authorization", `Bearer ${ACCESS}`);
  return new Request(`${BASE}${path}`, { ...init, headers });
}

// Replaces globalThis.fetch with a recorder; `reply` builds the upstream Response for a call.
function trapFetch(reply) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (request, options) => {
    const call = { url: new URL(String(request)), options };
    calls.push(call);
    return reply ? reply(call) : new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "content-type": "application/json" } });
  };
  return { calls, restore: () => { globalThis.fetch = original; } };
}

async function bodyBytes(options) {
  const body = options.body;
  if (body === undefined || body === null) return null;
  if (typeof body === "string") return new TextEncoder().encode(body);
  if (body instanceof Uint8Array) return body;
  return new Uint8Array(await new Response(body).arrayBuffer());
}

const GET = "GET";
const POST = "POST";
const ROUTES = [
  [POST, "/api/debug/sessions", "/v1/debug/sessions"],
  [GET, `/api/debug/sessions/${SESSION}`, `/v1/debug/sessions/${SESSION}`],
  [GET, `/api/debug/sessions/${SESSION}/stream`, `/v1/debug/sessions/${SESSION}/stream`],
  [GET, `/api/debug/sessions/${SESSION}/events`, `/v1/debug/sessions/${SESSION}/events`],
  [POST, `/api/debug/sessions/${SESSION}/resume`, `/v1/debug/sessions/${SESSION}/resume`],
  [POST, `/api/debug/sessions/${SESSION}/cancel`, `/v1/debug/sessions/${SESSION}/cancel`],
  [GET, `/api/debug/sessions/${SESSION}/raw`, `/v1/debug/sessions/${SESSION}/raw`],
  [GET, `/api/debug/sessions/${SESSION}/raw/12`, `/v1/debug/sessions/${SESSION}/raw/12`],
  [GET, `/api/debug/sessions/${SESSION}/suggestions`, `/v1/debug/sessions/${SESSION}/suggestions`],
  [POST, `/api/debug/sessions/${SESSION}/suggestions`, `/v1/debug/sessions/${SESSION}/suggestions`],
  [POST, "/api/debug/test-rule", "/v1/debug/test-rule"],
  [POST, "/api/debug/preview", "/v1/debug/preview"],
  [POST, "/api/debug/diff", "/v1/debug/diff"],
  [POST, "/api/debug/export", "/v1/debug/export"],
];

test("every workbench route maps /api/debug/* to /v1/debug/* with the backend token injected", async () => {
  const trap = trapFetch();
  try {
    for (const [method, path, upstream] of ROUTES) {
      trap.calls.length = 0;
      const init = method === POST ? { method, headers: { "content-type": "application/json" }, body: "{}" } : { method };
      const response = await worker.fetch(authed(path, init), env);
      assert.equal(response.status, 200, `${method} ${path}`);
      assert.equal(trap.calls.length, 1, `${method} ${path}`);
      const { url, options } = trap.calls[0];
      assert.equal(url.origin, "https://validator.example");
      assert.equal(url.pathname, upstream, `${method} ${path}`);
      assert.equal(options.method, method);
      assert.equal(options.headers.get("x-api-key"), "server-secret");
    }
  } finally {
    trap.restore();
  }
});

test("the browser Authorization header, cookies and a caller x-api-key never reach the backend", async () => {
  const trap = trapFetch();
  try {
    await worker.fetch(authed(`/api/debug/sessions/${SESSION}/stream`, { headers: { cookie: "sid=1", "x-api-key": "attacker" } }), env);
    await worker.fetch(authed("/api/debug/test-rule", { method: POST, headers: { "content-type": "application/json", cookie: "sid=1", "x-api-key": "attacker" }, body: "{}" }), env);
    assert.equal(trap.calls.length, 2);
    for (const { options } of trap.calls) {
      assert.equal(options.headers.get("authorization"), null, "the access token stops at the Worker");
      assert.equal(options.headers.get("cookie"), null);
      assert.equal(options.headers.get("x-api-key"), "server-secret");
    }
  } finally {
    trap.restore();
  }
});

test("query strings are kept on GET only (after / limit / maxBytes / source)", async () => {
  const trap = trapFetch();
  try {
    await worker.fetch(authed(`/api/debug/sessions/${SESSION}/events?after=7&limit=50`), env);
    await worker.fetch(authed(`/api/debug/sessions/${SESSION}/raw/3?maxBytes=262144`), env);
    await worker.fetch(authed(`/api/debug/sessions/${SESSION}/suggestions?source=working`), env);
    await worker.fetch(authed("/api/debug/diff?evil=1", { method: POST, headers: { "content-type": "application/json" }, body: "{}" }), env);
    assert.equal(trap.calls[0].url.search, "?after=7&limit=50");
    assert.equal(trap.calls[1].url.search, "?maxBytes=262144");
    assert.equal(trap.calls[2].url.search, "?source=working");
    assert.equal(trap.calls[3].url.search, "");
  } finally {
    trap.restore();
  }
});

test("POST bodies are forwarded byte for byte (not parsed or re-serialised) as application/json", async () => {
  const trap = trapFetch();
  try {
    const exact = '{ "source" : {"bookSourceUrl":"https://a.example","note":"中文 \\u00e9"},   "key":"我的" }';
    await worker.fetch(authed("/api/debug/sessions", { method: POST, headers: { "content-type": "text/plain" }, body: exact }), env);
    const forwarded = trap.calls[0].options;
    assert.equal(new TextDecoder().decode(await bodyBytes(forwarded)), exact);
    assert.equal(forwarded.headers.get("content-type"), "application/json");
    // not even valid JSON is rewritten: validation belongs to the backend (it answers 400)
    trap.calls.length = 0;
    await worker.fetch(authed("/api/debug/diff", { method: POST, headers: { "content-type": "application/json" }, body: "{not json" }), env);
    assert.equal(new TextDecoder().decode(await bodyBytes(trap.calls[0].options)), "{not json");
  } finally {
    trap.restore();
  }
});

function sseUpstream() {
  let controller;
  const encoder = new TextEncoder();
  const stream = new ReadableStream({ start(c) { controller = c; } });
  const response = new Response(stream, { status: 200, headers: { "content-type": "text/event-stream; charset=utf-8", "cache-control": "no-store" } });
  // The Worker must never consume the body itself.
  response.json = async () => { throw new Error("SSE body must not be JSON-parsed"); };
  response.text = async () => { throw new Error("SSE body must not be buffered"); };
  return { response, push: (text) => controller.enqueue(encoder.encode(text)), close: () => controller.close() };
}

test("SSE is streamed through unparsed and unbuffered with the right headers", async () => {
  const upstream = sseUpstream();
  const trap = trapFetch(() => upstream.response);
  try {
    const response = await worker.fetch(authed(`/api/debug/sessions/${SESSION}/stream?after=0`, { headers: { "last-event-id": "4" } }), env);
    assert.equal(response.status, 200);
    assert.match(response.headers.get("content-type"), /^text\/event-stream/);
    assert.equal(response.headers.get("x-accel-buffering"), "no");
    assert.equal(response.headers.get("cache-control"), "no-store");
    const { options, url } = trap.calls[0];
    assert.equal(url.pathname, `/v1/debug/sessions/${SESSION}/stream`);
    assert.equal(url.search, "?after=0");
    assert.equal(options.headers.get("accept"), "text/event-stream");
    assert.equal(options.headers.get("last-event-id"), "4");
    assert.equal(options.headers.get("authorization"), null);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    // The first frame is readable while the upstream stream is still open: nothing is buffered until the end.
    upstream.push(": connected\n\n");
    let chunk = await reader.read();
    assert.equal(decoder.decode(chunk.value), ": connected\n\n");
    const frame = 'id: 1\nevent: trace\ndata: {"seq":1,"text":"[00:00.000] ⇒开始"}\n\n';
    upstream.push(frame);
    chunk = await reader.read();
    assert.equal(decoder.decode(chunk.value), frame);
    upstream.push('event: done\ndata: {"status":"pass"}\n\n');
    upstream.close();
    chunk = await reader.read();
    assert.match(decoder.decode(chunk.value), /event: done/);
    assert.equal((await reader.read()).done, true);
  } finally {
    trap.restore();
  }
});

test("a browser that disconnects aborts the upstream stream (request.signal is forwarded for /stream only)", async () => {
  const trap = trapFetch();
  try {
    const controller = new AbortController();
    await worker.fetch(authed(`/api/debug/sessions/${SESSION}/stream`, { signal: controller.signal }), env);
    await worker.fetch(authed(`/api/debug/sessions/${SESSION}/events`, { signal: controller.signal }), env);
    assert.ok(trap.calls[0].options.signal, "stream request carries the abort signal");
    assert.equal(trap.calls[1].options.signal, undefined);
  } finally {
    trap.restore();
  }
});

test("debug JSON responses and errors pass through untouched (no job-style field rewriting)", async () => {
  const events = { items: [{ seq: 1, created_at: "x", payload_json: "{\"a\":1}", text: "hi" }], done: false, status: "running", nextAfter: 1 };
  const trap = trapFetch((call) => {
    if (call.url.pathname.endsWith("/events")) return new Response(JSON.stringify(events), { headers: { "content-type": "application/json" } });
    return new Response(JSON.stringify({ error: "debug busy", retryAfter: 10 }), { status: 429, headers: { "content-type": "application/json", "retry-after": "10" } });
  });
  try {
    const polled = await worker.fetch(authed(`/api/debug/sessions/${SESSION}/events?after=0`), env);
    assert.deepEqual(await polled.json(), events); // snake_case keys and payload_json are NOT translated
    const busy = await worker.fetch(authed("/api/debug/sessions", { method: POST, headers: { "content-type": "application/json" }, body: "{}" }), env);
    assert.equal(busy.status, 429);
    assert.equal(busy.headers.get("retry-after"), "10");
    assert.deepEqual(await busy.json(), { error: "debug busy", retryAfter: 10 });
    assert.match(busy.headers.get("access-control-expose-headers"), /retry-after/);
  } finally {
    trap.restore();
  }
});

test("workbench routes need the access token, and nothing reaches the backend without it", async () => {
  const trap = trapFetch();
  try {
    for (const [method, path] of ROUTES) {
      const bare = await worker.fetch(new Request(`${BASE}${path}`, { method, ...(method === POST ? { body: "{}" } : {}) }), env);
      assert.equal(bare.status, 401, `${method} ${path}`);
      assert.match(bare.headers.get("www-authenticate"), /^Bearer/);
      const wrong = await worker.fetch(new Request(`${BASE}${path}`, { method, headers: { authorization: "Bearer nope", "x-api-key": "server-secret" }, ...(method === POST ? { body: "{}" } : {}) }), env);
      assert.equal(wrong.status, 401, `${method} ${path}`);
    }
    assert.equal(trap.calls.length, 0);
    const open = { ...env };
    delete open.READORI_VALIDATOR_ACCESS_TOKEN;
    const failClosed = await worker.fetch(authed("/api/debug/diff", { method: POST, body: "{}" }), open);
    assert.equal(failClosed.status, 503);
    assert.equal(trap.calls.length, 0);
  } finally {
    trap.restore();
  }
});

test("oversized workbench bodies are refused with 413 before the backend is called", async () => {
  const trap = trapFetch();
  try {
    const limit = 2 * 1024 * 1024;
    // declared length
    const declared = await worker.fetch(authed("/api/debug/test-rule", { method: POST, headers: { "content-type": "application/json", "content-length": String(limit + 1) }, body: "{}" }), env);
    assert.equal(declared.status, 413);
    // no content-length: the stream is counted while it is read
    let pulled = 0;
    const chunk = new Uint8Array(256 * 1024).fill(120);
    const stream = new ReadableStream({
      pull(controller) {
        if (pulled >= 40) return controller.close();
        pulled += 1;
        controller.enqueue(chunk);
      },
    });
    const chunked = await worker.fetch(authed("/api/debug/export", { method: POST, headers: { "content-type": "application/json" }, body: stream, duplex: "half" }), env);
    assert.equal(chunked.status, 413);
    assert.ok(pulled <= 10, `the Worker stopped reading early (read ${pulled} chunks of 40)`);
    assert.equal(trap.calls.length, 0);
    // exactly at the limit is fine
    const exact = new Uint8Array(limit).fill(32);
    const ok = await worker.fetch(authed("/api/debug/diff", { method: POST, headers: { "content-type": "application/json" }, body: exact }), env);
    assert.equal(ok.status, 200);
    assert.equal((await bodyBytes(trap.calls[0].options)).byteLength, limit);
  } finally {
    trap.restore();
  }
});

test("unknown workbench paths and wrong methods are rejected before reaching the backend", async () => {
  const trap = trapFetch();
  try {
    const cases = [
      [GET, "/api/debug", 404],
      [GET, "/api/debug/", 404],
      [GET, "/api/debug/unknown", 404],
      [POST, "/api/debug/sessions/x", 404], // not a valid session id
      [GET, "/api/debug/sessions/short/events", 404],
      [GET, `/api/debug/sessions/${SESSION}/nope`, 404],
      [GET, `/api/debug/sessions/${SESSION}/events/extra`, 404],
      [GET, `/api/debug/sessions/${SESSION}/raw/abc`, 404],
      [GET, `/api/debug/sessions/${SESSION}/raw/1/2`, 404],
      [GET, `/api/debug/sessions/${SESSION}/raw/1%2f2`, 404],
      [GET, `/api/debug/sessions/${"x".repeat(80)}`, 404],
      [GET, "/api/debug/diff", 405],
      [GET, "/api/debug/test-rule", 405],
      [GET, "/api/debug/sessions", 405],
      [POST, `/api/debug/sessions/${SESSION}`, 405],
      [GET, `/api/debug/sessions/${SESSION}/resume`, 405],
      [GET, `/api/debug/sessions/${SESSION}/cancel`, 405],
      [POST, `/api/debug/sessions/${SESSION}/stream`, 405],
      [POST, `/api/debug/sessions/${SESSION}/raw`, 405],
      ["DELETE", `/api/debug/sessions/${SESSION}`, 405],
    ];
    for (const [method, path, status] of cases) {
      const response = await worker.fetch(authed(path, { method }), env);
      assert.equal(response.status, status, `${method} ${path}`);
    }
    assert.equal(trap.calls.length, 0);
  } finally {
    trap.restore();
  }
});

test("an unreachable backend is a 502 for the workbench too", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => { throw new TypeError("connect failed"); };
  try {
    const response = await worker.fetch(authed(`/api/debug/sessions/${SESSION}/events`), env);
    assert.equal(response.status, 502);
    assert.equal((await response.json()).error, "validator server unavailable");
  } finally {
    globalThis.fetch = original;
  }
});

test("batch routes are unchanged by the workbench routes", async () => {
  const trap = trapFetch();
  try {
    const job = await worker.fetch(authed("/api/jobs", { method: POST, headers: { "content-type": "application/json" }, body: JSON.stringify({ inputName: "a.json", sources: [] }) }), env);
    assert.equal(job.status, 200);
    assert.equal(trap.calls[0].url.pathname, "/v1/jobs");
    assert.equal(JSON.parse(new TextDecoder().decode(await bodyBytes(trap.calls[0].options))).input_name, "a.json");
    assert.equal((await worker.fetch(authed("/api/jobs/job-1/anything"), env)).status, 404);
  } finally {
    trap.restore();
  }
});
