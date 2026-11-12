// Рабочий агент ядра поверх @earendil-works/pi-coding-agent (встроенные инструменты pi: чтение, поиск, правка, команды).
// Протокол: строки JSON, stdin от вызывающей стороны, stdout к ней, журнал только в stderr.
// БЕЗОПАСНОСТЬ: каждый вызов встроенного инструмента до исполнения уходит вызывающей стороне (`permission`) и исполняется
// только после `permission_result` с allow:true. Без ответа инструмент не исполняется никогда. Из домашней папки
// пользователя ничего не подхватывается: настройки, расширения, скиллы, контекстные файлы и хранилище входа pi берутся
// только из явных путей (`auth_file`, `skills_dir`), служебная папка pi временная.
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

const NO_LOGIN = "нет входа: выполни `npx @earendil-works/pi-ai login openai` в папке services/agent-runtime";

export function createWorker({ input, output, log = () => {}, onModelRequest = null }) {
  const send = (obj) => { output.write(JSON.stringify(obj) + "\n"); };
  const pendingCustom = new Map(); // call_id -> { resolve, reject }
  const pendingPerm = new Map(); // call_id -> resolve(решение)
  let cfg = null;
  let session = null;
  let agentDir = null;
  let turn = null; // { id, promise, aborted, maxed, requests, input, output }
  let closing = false;
  let finish;
  const done = new Promise((r) => { finish = r; });
  let customNames = new Set();

  const textOf = (content) =>
    typeof content === "string" ? content : (content || []).filter((c) => c.type === "text").map((c) => c.text).join("");

  /** Вызов встроенного инструмента: решает вызывающая сторона; любой сбой или отсутствие хода это отказ. */
  function permissionGate(pi) {
    pi.on("tool_call", async (event) => {
      if (customNames.has(event.toolName)) return undefined; // свой инструмент исполняет вызывающая сторона сам
      if (!turn || turn.aborted) return { block: true, reason: "прервано", terminate: true };
      const callId = event.toolCallId;
      const decision = await new Promise((resolve) => {
        pendingPerm.set(callId, resolve);
        send({ type: "permission", id: turn.id, call_id: callId, tool: event.toolName, input: event.input ?? {} });
      });
      pendingPerm.delete(callId);
      if (decision?.allow !== true) return { block: true, reason: String(decision?.reason || "отказано") };
      send({ type: "tool_use", id: turn?.id ?? null, tool: event.toolName, input: event.input ?? {} });
      return undefined;
    });
  }

  function customToolDefs() {
    return cfg.custom_tools.map((t) => ({
      name: t.name,
      label: t.name,
      description: t.description || "",
      parameters: t.parameters || { type: "object", properties: {} },
      execute: (callId, params, signal) => new Promise((resolve, reject) => {
        const fail = (e) => { pendingCustom.delete(callId); reject(e); };
        if (signal?.aborted) return fail(new Error("aborted"));
        signal?.addEventListener("abort", () => fail(new Error("aborted")), { once: true });
        pendingCustom.set(callId, {
          resolve: (r) => {
            pendingCustom.delete(callId);
            if (r.ok) resolve({ content: [{ type: "text", text: String(r.text ?? "") }], details: {} });
            else reject(new Error(String(r.text ?? "ошибка инструмента")));
          },
          reject: fail,
        });
        send({ type: "tool_call", id: turn?.id ?? null, call_id: callId, name: t.name, args: params ?? {} });
      }),
    }));
  }

  function rejectAll(reason) {
    for (const r of [...pendingPerm.values()]) r({ allow: false, reason });
    for (const p of [...pendingCustom.values()]) p.reject(new Error("aborted"));
  }

  const readSystem = (m) => (m.system_file ? readFileSync(m.system_file, "utf-8") : m.system || "");

  async function init(m) {
    if (session) return send({ type: "error", message: "уже инициализирован", fatal: false });
    try {
      cfg = {
        thinking: m.thinking || "off",
        session_file: m.session_file || null,
        max_turns: m.max_turns || 0,
        custom_tools: m.custom_tools || [],
        tools: m.tools || [],
      };
      customNames = new Set(cfg.custom_tools.map((t) => t.name));
      const system = readSystem(m);
      // Служебная папка pi временная и пустая: домашняя папка пользователя не читается.
      agentDir = mkdtempSync(join(tmpdir(), "gits-worker-"));
      process.env.PI_CODING_AGENT_DIR = agentDir;
      for (const k of Object.keys(process.env)) {
        if (/(_API_KEY|_TOKEN|_SECRET)$/i.test(k)) delete process.env[k]; // вход только из auth_file
      }
      const { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } =
        await import("@earendil-works/pi-coding-agent");
      const cwd = m.cwd || process.cwd();

      let model;
      let modelRuntime;
      if (m.provider === "faux") {
        const { fauxAssistantMessage, fauxProvider, fauxText, fauxToolCall } =
          await import("@earendil-works/pi-ai/providers/faux");
        const faux = fauxProvider({ tokensPerSecond: m.faux_tps || 5000 });
        // onModelRequest: только для тестов, видит всё, что уходит модели (подсказка, сообщения, инструменты)
        faux.setResponses((m.faux_responses || []).map((s) => (context) => {
          if (onModelRequest) onModelRequest(context);
          return fauxAssistantMessage([
            ...(s.text ? [fauxText(s.text)] : []),
            ...(s.tool_calls || []).map((c) => fauxToolCall(c.name, c.args || {})),
          ]);
        }));
        modelRuntime = await ModelRuntime.create({
          authPath: join(agentDir, "auth.json"), modelsPath: null, refreshOnCreate: false, allowModelNetwork: false,
        });
        modelRuntime.registerNativeProvider(faux.provider);
        model = faux.getModel();
        await modelRuntime.setRuntimeApiKey(model.provider, "faux");
      } else if (m.provider === "openai") {
        let has = false;
        try { has = Boolean(JSON.parse(readFileSync(m.auth_file, "utf-8")).openai); } catch { has = false; }
        if (!m.auth_file || !existsSync(m.auth_file) || !has) {
          return send({ type: "error", fatal: true, message: NO_LOGIN });
        }
        modelRuntime = await ModelRuntime.create({
          authPath: m.auth_file, modelsPath: null, refreshOnCreate: false, allowModelNetwork: false,
        });
        model = modelRuntime.getModel("openai", m.model);
        if (!model) return send({ type: "error", fatal: true, message: `нет модели ${m.model}` });
      } else {
        return send({ type: "error", fatal: true, message: `неизвестный поставщик: ${m.provider}` });
      }

      const settingsManager = SettingsManager.inMemory({
        compaction: { enabled: false },
        retry: { enabled: false },
      });
      const loader = new DefaultResourceLoader({
        cwd,
        agentDir,
        settingsManager,
        noExtensions: true,
        noSkills: !m.skills_dir,
        noPromptTemplates: true,
        noThemes: true,
        noContextFiles: true,
        additionalSkillPaths: m.skills_dir ? [m.skills_dir] : [],
        extensionFactories: [permissionGate],
        systemPrompt: system,
        appendSystemPrompt: [],
      });
      await loader.reload();

      const sessionManager = cfg.session_file
        ? SessionManager.open(cfg.session_file, undefined, cwd)
        : SessionManager.inMemory(cwd);
      const enabled = [...cfg.tools, ...customNames];
      const created = await createAgentSession({
        cwd,
        agentDir,
        model,
        thinkingLevel: cfg.thinking,
        modelRuntime,
        resourceLoader: loader,
        settingsManager,
        sessionManager,
        tools: enabled,
        customTools: customToolDefs(),
      });
      session = created.session;
      // страховка: активными остаются только заказанные инструменты
      const active = session.getActiveToolNames();
      if (active.some((n) => !enabled.includes(n))) session.setActiveToolsByName(active.filter((n) => enabled.includes(n)));
      session.subscribe(onEvent);
      send({ type: "ready", restored: session.messages.length, tools: session.getActiveToolNames() });
    } catch (e) {
      send({ type: "error", fatal: true, message: `init: ${e.message}` });
    }
  }

  function onEvent(e) {
    if (!turn) return;
    if (e.type === "message_update" && e.assistantMessageEvent.type === "text_delta") {
      send({ type: "text_delta", id: turn.id, delta: e.assistantMessageEvent.delta });
    } else if (e.type === "message_end" && e.message.role === "assistant") {
      const text = textOf(e.message.content);
      if (text) send({ type: "text", id: turn.id, text });
      if (e.message.stopReason === "aborted") return; // обрыв своего же abort не считается обращением
      turn.requests += 1;
      turn.input += e.message.usage?.input || 0;
      turn.output += e.message.usage?.output || 0;
      const wantsTools = (e.message.content || []).some((c) => c.type === "toolCall");
      if (cfg.max_turns && turn.requests >= cfg.max_turns && wantsTools && !turn.aborted) {
        turn.maxed = true;
        turn.aborted = true;
        rejectAll("предел обращений к модели");
        session.abort().catch(() => {});
      }
    }
  }

  function startTurn(m) {
    if (turn) return send({ type: "error", id: m.id, message: "busy", fatal: false });
    const t = { id: m.id, aborted: false, maxed: false, requests: 0, input: 0, output: 0 };
    turn = t;
    t.promise = (async () => {
      let error = null;
      try {
        await session.prompt(String(m.text ?? ""));
      } catch (e) {
        error = e.message || String(e);
      }
      let status = "ok";
      const last = [...session.messages].reverse().find((x) => x.role === "assistant");
      if (t.maxed) status = "max_turns";
      else if (t.aborted || last?.stopReason === "aborted") status = "aborted";
      else if (error || last?.stopReason === "error") { status = "error"; error = error || last?.errorMessage || "ошибка поставщика"; }
      turn = null;
      send({
        type: "end", id: t.id, status, error: status === "error" ? error : null, turns: t.requests,
        usage: { requests: t.requests, input: t.input, output: t.output },
      });
    })();
  }

  async function abort() {
    if (!turn) return;
    turn.aborted = true;
    rejectAll("прервано");
    try { await session.abort(); } catch (e) { log(`abort: ${e.message}`); }
    await turn.promise;
  }

  async function shutdown() {
    if (closing) return;
    closing = true;
    try {
      if (turn) await abort();
      if (session) session.dispose();
    } catch (e) {
      log(`shutdown: ${e.message}`);
    }
    try { if (agentDir) rmSync(agentDir, { recursive: true, force: true }); } catch { /* не критично */ }
    rl.close();
    finish();
  }

  const fail = (e) => send({ type: "error", message: e.message || String(e), fatal: false });

  async function handle(m) {
    if (m.type === "init") return init(m);
    if (!session && m.type !== "shutdown") return send({ type: "error", message: "нет init", fatal: false });
    switch (m.type) {
      case "prompt": return startTurn(m);
      case "steer":
        if (!turn) return send({ type: "error", message: "idle", fatal: false });
        return session.steer(String(m.text ?? ""));
      case "abort": return abort();
      case "permission_result": {
        const r = pendingPerm.get(m.call_id);
        if (r) r(m);
        else log(`permission_result без ожидающего вызова: ${m.call_id}`);
        return;
      }
      case "tool_result": {
        const p = pendingCustom.get(m.call_id);
        if (p) p.resolve(m);
        else log(`tool_result без ожидающего вызова: ${m.call_id}`);
        return;
      }
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
    // abort и ответы идут мимо очереди: они не должны ждать долгую обработку
    if (m.type === "abort" || m.type === "permission_result" || m.type === "tool_result") {
      Promise.resolve(handle(m)).catch(fail);
      return;
    }
    chain = chain.then(() => handle(m)).catch(fail);
  });
  rl.on("close", () => { chain.then(shutdown); });

  return { done, close: shutdown };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const worker = createWorker({
    input: process.stdin,
    output: process.stdout,
    log: (s) => process.stderr.write(`[agent-worker] ${s}\n`),
  });
  worker.done.then(() => {
    process.exitCode = 0;
    // страховка: если соединения не закрылись сами, выходим позже, не обрывая закрытие
    setTimeout(() => process.exit(0), 5000).unref();
  });
}
