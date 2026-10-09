const http = require("node:http");
const fs = require("node:fs/promises");
const path = require("node:path");
const crypto = require("node:crypto");

const PORT = Number(process.env.PORT || 3000);
const TOKEN = process.env.COPIER_TOKEN || "";
const DATA_DIR = process.env.DATA_DIR || "/data";
const STATE_FILE = path.join(DATA_DIR, "queue.json");
const MAX_EVENTS = 50000;

let state = { nextId: 1, events: [], cursors: {}, seen: {} };
let writeChain = Promise.resolve();

function send(res, status, data) {
  const body = JSON.stringify(data);
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "content-length": Buffer.byteLength(body) });
  res.end(body);
}
function readJson(req) {
  return new Promise((resolve, reject) => {
    let raw = "";
    req.on("data", chunk => {
      raw += chunk;
      if (raw.length > 1024 * 1024) {
        reject(new Error("request body too large"));
        req.destroy();
      }
    });
    req.on("end", () => {
      try { resolve(JSON.parse(raw || "{}")); } catch { reject(new Error("invalid JSON")); }
    });
    req.on("error", reject);
  });
}
function authorized(req) {
  if (!TOKEN) return false;
  const supplied = req.headers.authorization || "";
  const a = Buffer.from(supplied);
  const b = Buffer.from("Bearer " + TOKEN);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
async function saveState() {
  await fs.mkdir(DATA_DIR, { recursive: true });
  const temp = STATE_FILE + ".tmp";
  await fs.writeFile(temp, JSON.stringify(state), { mode: 0o600 });
  await fs.rename(temp, STATE_FILE);
}
function mutate(fn) {
  const task = writeChain.then(async () => {
    const result = await fn();
    await saveState();
    return result;
  });
  writeChain = task.catch(() => {});
  return task;
}
async function loadState() {
  await fs.mkdir(DATA_DIR, { recursive: true });
  try {
    const parsed = JSON.parse(await fs.readFile(STATE_FILE, "utf8"));
    if (parsed && Array.isArray(parsed.events) && parsed.cursors && parsed.seen) {
      state = { nextId: Number(parsed.nextId) || 1, events: parsed.events, cursors: parsed.cursors, seen: parsed.seen };
    }
  } catch (e) {
    if (e.code !== "ENOENT") throw e;
    await saveState();
  }
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || "/", "http://localhost");
  if (req.method === "GET" && url.pathname === "/health") {
    return send(res, 200, { ok: true, service: "mt5-reverse-copier", storage: "persistent-volume", authenticated: Boolean(TOKEN), queue_depth: state.events.length });
  }
  if (!authorized(req)) return send(res, 401, { error: "unauthorized" });
  try {
    if (req.method === "POST" && url.pathname === "/master/event") {
      const body = await readJson(req);
      const externalId = String(body.event_id || body.signal_id || "").trim();
      const symbol = String(body.symbol || "").trim().toUpperCase();
      const action = String(body.action || body.side || "").trim().toUpperCase();
      if (!externalId || externalId.length > 200) return send(res, 400, { error: "event_id required (max 200 chars)" });
      if (!symbol || symbol.length > 50) return send(res, 400, { error: "symbol required (max 50 chars)" });
      if (!["BUY", "SELL", "CLOSE", "CLOSE_ALL", "HEARTBEAT"].includes(action)) return send(res, 400, { error: "action must be BUY, SELL, CLOSE, CLOSE_ALL, or HEARTBEAT" });
      if (state.events.length >= MAX_EVENTS) return send(res, 503, { error: "queue capacity reached; pause master and inspect slave acknowledgements" });
      const result = await mutate(async () => {
        if (state.seen[externalId]) return { duplicate: true, id: state.seen[externalId] };
        const id = state.nextId++;
        const reverseAction = action === "BUY" ? "SELL" : action === "SELL" ? "BUY" : action;
        const event = { id, external_event_id: externalId, master_ticket: body.master_ticket == null ? null : String(body.master_ticket), symbol, action, reverse_action: reverseAction, volume: Number.isFinite(Number(body.volume)) && body.volume != null ? Number(body.volume) : null, payload: body, created_at: new Date().toISOString() };
        state.events.push(event);
        state.seen[externalId] = id;
        return { duplicate: false, id, reverse_action: reverseAction };
      });
      return send(res, result.duplicate ? 200 : 201, { accepted: true, ...result });
    }
    if (req.method === "GET" && url.pathname === "/slave/next") {
      const client = String(url.searchParams.get("client") || "slave-1").slice(0, 100);
      const cursor = Number(state.cursors[client] || 0);
      const event = state.events.find(item => item.id > cursor);
      return send(res, 200, { client_id: client, event: event || null });
    }
    if (req.method === "POST" && url.pathname === "/slave/ack") {
      const body = await readJson(req);
      const client = String(body.client_id || "slave-1").slice(0, 100);
      const id = Number(body.event_id);
      if (!Number.isSafeInteger(id) || id < 1) return send(res, 400, { error: "numeric internal event_id required" });
      const result = await mutate(async () => {
        const exists = state.events.some(item => item.id === id);
        if (!exists) return { acknowledged: false, error: "event not found" };
        state.cursors[client] = Math.max(Number(state.cursors[client] || 0), id);
        return { acknowledged: true, client_id: client, event_id: id };
      });
      return send(res, result.acknowledged ? 200 : 404, result);
    }
    if (req.method === "GET" && url.pathname === "/slave/status") {
      const client = String(url.searchParams.get("client") || "slave-1").slice(0, 100);
      const cursor = Number(state.cursors[client] || 0);
      return send(res, 200, { client_id: client, cursor, queue_depth: state.events.filter(item => item.id > cursor).length, total_events: state.events.length });
    }
    return send(res, 404, { error: "not_found" });
  } catch (error) {
    return send(res, 500, { error: "internal_error", detail: String(error && error.message || error) });
  }
});
loadState().then(() => server.listen(PORT, "0.0.0.0", () => console.log("MT5 reverse copier listening on " + PORT))).catch(error => {
  console.error("Failed to initialize persistent queue", error);
  process.exit(1);
});
