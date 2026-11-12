// Быстрая модель ядра: один вопрос, один ответ, без агента и без инструментов (pi-ai напрямую).
// Протокол: строки JSON, stdin от ядра, stdout к ядру, журнал только в stderr. Запросы идут параллельно.
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { createModels } from "@earendil-works/pi-ai";
import { openaiProvider } from "@earendil-works/pi-ai/providers/openai";
import { fauxAssistantMessage, fauxProvider, fauxText } from "@earendil-works/pi-ai/providers/faux";

const textOf = (content) =>
  typeof content === "string" ? content : (content || []).filter((c) => c.type === "text").map((c) => c.text).join("");

/** Хранилище входа в файле (как в bridge.mjs). Содержимое не логируется. */
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

/** Шаг поддельного поставщика: `text`, `error` (сбой поставщика) или `echo` (ответ описывает полученный запрос). */
function fauxStep(s) {
  if (s.echo) {
    return (context) => {
      const last = [...context.messages].reverse().find((m) => m.role === "user");
      const blocks = typeof last?.content === "string" ? [{ type: "text", text: last.content }] : (last?.content || []);
      const kinds = blocks.map((b) => (b.type === "image" ? `image:${b.mimeType}:${b.data.length}` : "text"));
      const sys = context.messages.find((m) => m.role === "system");
      return fauxAssistantMessage([fauxText(`${textOf(blocks)}|${kinds.join(",")}|sys=${sys ? textOf(sys.content) : ""}`)]);
    };
  }
  if (s.error) return fauxAssistantMessage([], { stopReason: "error", errorMessage: String(s.error) });
  return fauxAssistantMessage([fauxText(s.text || "")]);
}

export function createLlm({ input, output, log = () => {} }) {
  const send = (obj) => { output.write(JSON.stringify(obj) + "\n"); };
  const active = new Map(); // id -> AbortController
  let models = null;
  let fauxModel = null;
  let closing = false;
  let finish;
  const done = new Promise((r) => { finish = r; });

  function init(m) {
    if (models) return send({ type: "error", message: "уже инициализирован", fatal: false });
    try {
      if (m.provider === "faux") {
        const faux = fauxProvider({ tokensPerSecond: m.faux_tps || 5000 });
        faux.setResponses((m.faux_responses || []).map(fauxStep));
        models = createModels();
        models.setProvider(faux.provider);
        fauxModel = faux.getModel();
      } else if (m.provider === "openai") {
        const creds = fileCredentials(m.auth_file);
        if (!creds.has("openai")) {
          return send({ type: "error", fatal: true, message: "нет входа: выполни `npx @earendil-works/pi-ai login openai` в папке services/agent-runtime" });
        }
        models = createModels({ credentials: creds.store });
        models.setProvider(openaiProvider());
      } else {
        return send({ type: "error", fatal: true, message: `неизвестный поставщик: ${m.provider}` });
      }
      send({ type: "ready" });
    } catch (e) {
      send({ type: "error", fatal: true, message: `init: ${e.message}` });
    }
  }

  async function complete(m) {
    const id = m.id;
    if (active.has(id)) return send({ type: "failed", id, error: "повторный id" });
    const ctl = new AbortController();
    active.set(id, ctl);
    try {
      const model = fauxModel || models.getModel("openai", m.model);
      if (!model) return send({ type: "failed", id, error: `нет модели ${m.model}` });
      const content = m.image_b64
        ? [{ type: "image", data: m.image_b64, mimeType: m.image_mime || "image/jpeg" }, { type: "text", text: String(m.user ?? "") }]
        : String(m.user ?? "");
      const context = {
        messages: [
          ...(m.system ? [{ role: "system", content: String(m.system), timestamp: Date.now() }] : []),
          { role: "user", content, timestamp: Date.now() },
        ],
      };
      const options = { signal: ctl.signal };
      if (m.thinking && m.thinking !== "off") options.reasoning = m.thinking;
      const stream = models.streamSimple(model, context, options);
      let finalMsg = null;
      for await (const e of stream) {
        if (e.type === "text_delta" && m.stream) send({ type: "delta", id, text: e.delta });
        else if (e.type === "done") finalMsg = e.message;
        else if (e.type === "error") finalMsg = e.error;
      }
      if (ctl.signal.aborted || finalMsg?.stopReason === "aborted") return send({ type: "failed", id, error: "cancelled" });
      if (!finalMsg) return send({ type: "failed", id, error: "поставщик не вернул ответ" });
      if (finalMsg.stopReason === "error") return send({ type: "failed", id, error: finalMsg.errorMessage || "ошибка поставщика" });
      const u = finalMsg.usage || {};
      send({ type: "done", id, text: textOf(finalMsg.content), usage: { input: u.input || 0, output: u.output || 0 } });
    } catch (e) {
      send({ type: "failed", id, error: ctl.signal.aborted ? "cancelled" : (e.message || String(e)) });
    } finally {
      active.delete(id);
    }
  }

  async function shutdown() {
    if (closing) return;
    closing = true;
    for (const c of active.values()) c.abort();
    rl.close();
    finish();
  }

  const rl = createInterface({ input, crlfDelay: Infinity });
  rl.on("line", (line) => {
    if (!line.trim()) return;
    let m;
    try { m = JSON.parse(line); } catch { return send({ type: "error", message: "не JSON", fatal: false }); }
    switch (m.type) {
      case "init": return init(m);
      case "complete":
        if (!models) return send({ type: "failed", id: m.id, error: "нет init" });
        complete(m).catch((e) => log(`complete: ${e.message}`));
        return;
      case "cancel": active.get(m.id)?.abort(); return;
      case "shutdown": return void shutdown();
      default: return send({ type: "error", message: `неизвестный тип: ${m.type}`, fatal: false });
    }
  });
  rl.on("close", () => { shutdown(); });

  return { done, close: shutdown };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const llm = createLlm({ input: process.stdin, output: process.stdout, log: (s) => process.stderr.write(`[agent-llm] ${s}\n`) });
  llm.done.then(() => {
    process.exitCode = 0;
    setTimeout(() => process.exit(0), 5000).unref();
  });
}
