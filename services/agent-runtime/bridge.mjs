// Мост ядра к pi-agent-core. Протокол: строки JSON, stdin от ядра, stdout к ядру, журнал только в stderr.
// Инструменты исполняет ядро: execute шлёт tool_call и ждёт tool_result.
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { Agent } from "@earendil-works/pi-agent-core";
import { cleanupSessionResources, createModels } from "@earendil-works/pi-ai";
import { openaiProvider } from "@earendil-works/pi-ai/providers/openai";
import { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } from "@earendil-works/pi-ai/providers/faux";

const OMITTED = "[ранняя часть разговора опущена]";

const textOf = (content) =>
  typeof content === "string" ? content : (content || []).filter((c) => c.type === "text").map((c) => c.text).join("");

/** Хранилище входа в файле (формат тот же, что пишет `pi-ai login`). Содержимое не логируется. */
function fileCredentials(path) {
  const load = () => {
    try { return existsSync(path) ? JSON.parse(readFileSync(path, "utf-8")) : {}; } catch { return {}; }
  };
  const save = (all) => writeFileSync(path, JSON.stringify(all, null, 2), { mode: 0o600 });
  let chain = Promise.resolve();
  return {
    has: (id) => Boolean(load()[id]),
    store: {
      read: async (id) => load()[id],
      list: async () => Object.entries(load()).map(([providerId, c]) => ({ providerId, type: c.type })),
      modify: (id, fn) => (chain = chain.then(async () => {
        const all = load();
        const next = await fn(all[id]);
        if (next === undefined) delete all[id]; else all[id] = next;
        save(all);
        return next;
      })),
      delete: async (id) => { const all = load(); delete all[id]; save(all); },
    },
  };
}

/** Сжатие истории: старые сообщения до границы `user` заменяются одной пометкой. */
export function compactMessages(messages, maxChars) {
  const sizes = messages.map((m) => JSON.stringify(m).length);
  const total = sizes.reduce((a, b) => a + b, 0);
  if (!maxChars || total <= maxChars) return messages;
  const head = messages.findIndex((m) => m.role !== "system");
  if (head < 0) return messages;
  const target = maxChars * 0.6;
  let cut = -1; // первый индекс, с которого оставляем хвост (всегда сообщение user)
  let tail = 0;
  for (let i = messages.length - 1; i >= head; i--) {
    tail += sizes[i];
    if (messages[i].role === "user") {
      if (tail > target && cut >= 0) break;
      cut = i;
    }
  }
  if (cut <= head) return messages;
  const note = { role: "user", content: OMITTED, timestamp: Date.now() };
  return [...messages.slice(0, head), note, ...messages.slice(cut)];
}

export function createBridge({ input, output, log = () => {} }) {
  const send = (obj) => { output.write(JSON.stringify(obj) + "\n"); };
  const pending = new Map(); // call_id -> { resolve, reject }
  let cfg = null;
  let agent = null;
  let models = null;
  let turn = null; // { id, promise, aborted }
  let closing = false;
  let finish;
  const done = new Promise((r) => { finish = r; });

  const toolDefs = () => cfg.tools.map((t) => ({
    name: t.name,
    label: t.name,
    description: t.description || "",
    parameters: t.parameters || { type: "object", properties: {} },
    execute: (callId, params, signal) => new Promise((resolve, reject) => {
      const fail = (e) => { pending.delete(callId); reject(e); };
      if (signal?.aborted) return fail(new Error("aborted"));
      signal?.addEventListener("abort", () => fail(new Error("aborted")), { once: true });
      pending.set(callId, {
        resolve: (r) => {
          pending.delete(callId);
          if (r.ok) resolve({ content: [{ type: "text", text: String(r.text ?? "") }], details: {} });
          else reject(new Error(String(r.text ?? "ошибка инструмента")));
        },
        reject: fail,
      });
      send({ type: "tool_call", id: turn?.id ?? null, call_id: callId, name: t.name, args: params ?? {} });
    }),
  }));

  const stripSystem = (messages) => {
    let i = 0;
    while (i < messages.length && messages[i].role === "system") i++;
    return messages.slice(i);
  };

  function loadHistory() {
    if (!cfg.session_file || !existsSync(cfg.session_file)) return [];
    try {
      const data = JSON.parse(readFileSync(cfg.session_file, "utf-8"));
      return Array.isArray(data) ? stripSystem(data) : [];
    } catch (e) {
      log(`session_file не читается, начинаю с пустой истории: ${e.message}`);
      return [];
    }
  }

  function persist() {
    if (!cfg.session_file) return;
    try {
      mkdirSync(dirname(cfg.session_file), { recursive: true });
      const tmp = `${cfg.session_file}.tmp`;
      writeFileSync(tmp, JSON.stringify(agent.state.messages));
      renameSync(tmp, cfg.session_file);
    } catch (e) {
      log(`не удалось сохранить историю: ${e.message}`);
    }
  }

  function buildAgent(system, messages) {
    const a = new Agent({
      initialState: { systemPrompt: system, model: cfg.model, tools: toolDefs(), thinkingLevel: cfg.thinking, messages },
      streamFn: models.streamSimple.bind(models),
      toolExecution: "sequential",
      sessionId: cfg.session_id,
      transformContext: async (msgs) => compactMessages(msgs, cfg.max_context_chars),
    });
    a.subscribe((e) => {
      if (!turn) return;
      if (e.type === "message_update" && e.assistantMessageEvent.type === "text_delta") {
        send({ type: "text_delta", id: turn.id, delta: e.assistantMessageEvent.delta });
      } else if (e.type === "message_end" && e.message.role === "assistant") {
        const text = textOf(e.message.content);
        if (text) send({ type: "text", id: turn.id, text });
      }
    });
    return a;
  }

  async function init(m) {
    if (agent) return send({ type: "error", message: "уже инициализирован", fatal: false });
    try {
      cfg = {
        system: m.system || "", tools: m.tools || [], thinking: m.thinking || "off",
        session_file: m.session_file || null, session_id: m.session_id || "bridge",
        max_context_chars: m.max_context_chars || 0,
      };
      if (m.provider === "faux") {
        const faux = fauxProvider({ tokensPerSecond: m.faux_tps || 5000 });
        faux.setResponses((m.faux_responses || []).map((s) => fauxAssistantMessage([
          ...(s.text ? [fauxText(s.text)] : []),
          ...(s.tool_calls || []).map((c) => fauxToolCall(c.name, c.args || {})),
        ])));
        models = createModels();
        models.setProvider(faux.provider);
        cfg.model = faux.getModel();
      } else if (m.provider === "openai") {
        const creds = fileCredentials(m.auth_file);
        if (!creds.has("openai")) {
          return send({ type: "error", fatal: true, message: "нет входа: выполни `npx @earendil-works/pi-ai login openai` в папке services/agent-runtime" });
        }
        models = createModels({ credentials: creds.store });
        models.setProvider(openaiProvider());
        cfg.model = models.getModel("openai", m.model);
        if (!cfg.model) return send({ type: "error", fatal: true, message: `нет модели ${m.model}` });
      } else {
        return send({ type: "error", fatal: true, message: `неизвестный поставщик: ${m.provider}` });
      }
      const history = loadHistory();
      agent = buildAgent(cfg.system, history);
      send({ type: "ready", restored: history.length });
    } catch (e) {
      send({ type: "error", fatal: true, message: `init: ${e.message}` });
    }
  }

  function rejectPending() {
    for (const p of [...pending.values()]) p.reject(new Error("aborted"));
  }

  function startTurn(m) {
    if (turn) return send({ type: "error", id: m.id, message: "busy", fatal: false });
    const t = { id: m.id, aborted: false };
    turn = t;
    t.promise = (async () => {
      const from = agent.state.messages.length;
      let error = null;
      try {
        await agent.prompt(String(m.text ?? ""));
        await agent.waitForIdle();
      } catch (e) {
        error = e.message || String(e);
      }
      const added = agent.state.messages.slice(from).filter((x) => x.role === "assistant");
      const usage = { requests: added.length, input: 0, output: 0, cache_read: 0 };
      for (const x of added) {
        usage.input += x.usage?.input || 0;
        usage.output += x.usage?.output || 0;
        usage.cache_read += x.usage?.cacheRead || 0;
      }
      const last = added.at(-1);
      let status = "ok";
      if (t.aborted || last?.stopReason === "aborted") status = "aborted";
      else if (error || last?.stopReason === "error") { status = "error"; error = error || last.errorMessage || "ошибка поставщика"; }
      persist();
      turn = null;
      send({ type: "end", id: t.id, status, error: status === "error" ? error : null, usage });
    })();
  }

  async function abort() {
    if (!turn) return;
    turn.aborted = true;
    agent.abort();
    rejectPending();
    await turn.promise;
  }

  const userMsg = (text) => ({ role: "user", content: String(text ?? ""), timestamp: Date.now() });

  async function shutdown() {
    if (closing) return;
    closing = true;
    try {
      if (turn) await abort();
      if (agent) persist();
      if (cfg) cleanupSessionResources(cfg.session_id);
    } catch (e) {
      log(`shutdown: ${e.message}`);
    }
    rl.close();
    finish();
  }

  async function handle(m) {
    if (m.type === "init") return init(m);
    if (!agent && m.type !== "shutdown") return send({ type: "error", message: "нет init", fatal: false });
    switch (m.type) {
      case "prompt": return startTurn(m);
      case "steer":
        if (!turn) return send({ type: "error", message: "idle", fatal: false });
        return agent.steer(userMsg(m.text));
      case "follow_up":
        if (!turn) return send({ type: "error", message: "idle", fatal: false });
        return agent.followUp(userMsg(m.text));
      case "abort": return abort();
      case "tool_result": {
        const p = pending.get(m.call_id);
        if (p) p.resolve(m);
        else log(`tool_result без ожидающего вызова: ${m.call_id}`);
        return;
      }
      case "set_system":
        if (turn) return send({ type: "error", message: "busy", fatal: false });
        cfg.system = m.system || "";
        agent = buildAgent(cfg.system, stripSystem(agent.state.messages));
        return;
      case "reset":
        if (turn) await abort();
        agent = buildAgent(cfg.system, []);
        if (cfg.session_file) { try { rmSync(cfg.session_file, { force: true }); } catch (e) { log(`reset: ${e.message}`); } }
        return send({ type: "reset_done" });
      case "shutdown": return shutdown();
      default: return send({ type: "error", message: `неизвестный тип: ${m.type}`, fatal: false });
    }
  }

  const rl = createInterface({ input, crlfDelay: Infinity });
  let chain = Promise.resolve();
  rl.on("line", (line) => {
    if (!line.trim()) return;
    let m;
    try { m = JSON.parse(line); } catch { return send({ type: "error", message: "не JSON", fatal: false }); }
    // abort и tool_result нельзя ставить за долгой обработкой init/reset, но все обработчики быстрые: ход не ждётся
    chain = chain.then(() => handle(m)).catch((e) => send({ type: "error", message: e.message || String(e), fatal: false }));
  });
  rl.on("close", () => { chain.then(shutdown); });

  return { done, close: shutdown };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const bridge = createBridge({
    input: process.stdin,
    output: process.stdout,
    log: (s) => process.stderr.write(`[agent-runtime] ${s}\n`),
  });
  bridge.done.then(() => {
    process.exitCode = 0;
    // страховка: если соединения не закрылись сами, выходим позже, не обрывая закрытие
    setTimeout(() => process.exit(0), 5000).unref();
  });
}
