// Тесты рабочего агента на поддельном поставщике: без сети и без входа.
import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";
import { after, test } from "node:test";
import { createWorker } from "../worker.mjs";

const dirs = [];
const tmp = () => { const d = mkdtempSync(join(tmpdir(), "worker-test-")); dirs.push(d); return d; };
after(() => dirs.forEach((d) => rmSync(d, { recursive: true, force: true })));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const bash = (command) => ({ tool_calls: [{ name: "bash", args: { command } }] });
const shell = (path) => `node -e "require('fs').writeFileSync(process.argv[1],'x')" "${path.replaceAll("\\", "/")}"`;

function harness() {
  const input = new PassThrough();
  const output = new PassThrough();
  const out = [];
  const requests = []; // что ушло модели
  const waiters = [];
  let buf = "";
  output.on("data", (chunk) => {
    buf += chunk;
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i); buf = buf.slice(i + 1);
      if (!line) continue;
      const m = JSON.parse(line);
      out.push(m);
      for (const w of [...waiters]) {
        if (!m.__taken && w.pred(m)) { waiters.splice(waiters.indexOf(w), 1); m.__taken = true; w.resolve(m); }
      }
    }
  });
  const worker = createWorker({ input, output, log: () => {}, onModelRequest: (c) => requests.push(c) });
  const send = (obj) => input.write(JSON.stringify(obj) + "\n");
  const next = (type, extra = () => true, ms = 8000) => {
    const pred = (m) => m.type === type && extra(m);
    const seen = out.find((m) => !m.__taken && pred(m));
    if (seen) { seen.__taken = true; return Promise.resolve(seen); }
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error(`не дождались ${type}; получено: ${JSON.stringify(out)}`)), ms);
      waiters.push({ pred, resolve: (m) => { clearTimeout(t); resolve(m); } });
    });
  };
  const close = async () => { send({ type: "shutdown" }); await worker.done; };
  return { send, out, next, close, requests };
}

const init = (h, extra = {}) => {
  h.send({
    type: "init", provider: "faux", model: "faux", thinking: "off", system: "системная подсказка",
    cwd: tmp(), tools: ["read", "grep", "find", "ls"], custom_tools: [], skills_dir: null, session_file: null,
    max_turns: 0, faux_responses: [], ...extra,
  });
  return h.next("ready");
};
const systemOf = (ctx) => JSON.stringify(ctx.messages.filter((m) => m.role === "system"));
const toolNames = (ctx) => ctx.messages.flatMap((m) => (m.toolsAdded || []).map((t) => t.name));
const types = (h) => h.out.map((m) => m.type);

test("рукопожатие: ready, список включённых инструментов", async () => {
  const h = harness();
  const ready = await init(h, { tools: ["read", "ls"] });
  assert.deepEqual([...ready.tools].sort(), ["ls", "read"]);
  await h.close();
});

test("нет входа: фатальная ошибка", async () => {
  const h = harness();
  h.send({ type: "init", provider: "openai", model: "gpt-6-luna", auth_file: join(tmp(), "нет.json"), tools: [] });
  const err = await h.next("error");
  assert.equal(err.fatal, true);
  assert.match(err.message, /нет входа/);
  await h.close();
});

test("неизвестный поставщик: фатальная ошибка", async () => {
  const h = harness();
  h.send({ type: "init", provider: "x", tools: [] });
  assert.match((await h.next("error")).message, /неизвестный поставщик/);
  await h.close();
});

test("чтение файла из cwd: разрешение приходит до исполнения, итог доходит до модели", async () => {
  const cwd = tmp();
  writeFileSync(join(cwd, "note.txt"), "секретное-содержимое-42");
  const h = harness();
  await init(h, {
    cwd,
    faux_responses: [{ tool_calls: [{ name: "read", args: { path: "note.txt" } }] }, { text: "готово" }],
  });
  h.send({ type: "prompt", id: "1", text: "прочитай" });
  const perm = await h.next("permission");
  assert.equal(perm.tool, "read");
  assert.equal(perm.input.path, "note.txt");
  assert.ok(!types(h).includes("tool_use"), "tool_use до разрешения не шлётся");
  h.send({ type: "permission_result", call_id: perm.call_id, allow: true });
  const use = await h.next("tool_use");
  assert.equal(use.tool, "read");
  const end = await h.next("end");
  assert.equal(end.status, "ok");
  assert.equal(end.usage.requests, 2);
  assert.equal(end.turns, 2);
  const last = JSON.stringify(h.requests.at(-1).messages);
  assert.ok(last.includes("секретное-содержимое-42"), "модель увидела содержимое файла");
  assert.ok((await h.next("text")).text === "готово");
  await h.close();
});

test("отказ: инструмент не исполняется, модель видит причину", async () => {
  const cwd = tmp();
  const mark = join(cwd, "mark.txt");
  const h = harness();
  await init(h, { cwd, tools: ["bash"], faux_responses: [bash(shell(mark)), { text: "понял" }] });
  h.send({ type: "prompt", id: "1", text: "запиши" });
  const perm = await h.next("permission");
  assert.equal(perm.tool, "bash");
  h.send({ type: "permission_result", call_id: perm.call_id, allow: false, reason: "запрещено политикой X" });
  const end = await h.next("end");
  assert.equal(end.status, "ok");
  assert.equal(existsSync(mark), false);
  assert.ok(JSON.stringify(h.requests.at(-1).messages).includes("запрещено политикой X"));
  assert.ok(!types(h).includes("tool_use"));
  await h.close();
});

test("разрешённая команда исполняется ровно после ответа", async () => {
  const cwd = tmp();
  const mark = join(cwd, "mark.txt");
  const h = harness();
  await init(h, { cwd, tools: ["bash"], faux_responses: [bash(shell(mark)), { text: "ок" }] });
  h.send({ type: "prompt", id: "1", text: "запиши" });
  const perm = await h.next("permission");
  await sleep(400);
  assert.equal(existsSync(mark), false, "без ответа не исполнилось");
  h.send({ type: "permission_result", call_id: perm.call_id, allow: true });
  await h.next("end");
  assert.equal(existsSync(mark), true);
  await h.close();
});

test("без ответа инструмент не исполняется никогда; abort закрывает ход как отказ", async () => {
  const cwd = tmp();
  const mark = join(cwd, "mark.txt");
  const h = harness();
  await init(h, { cwd, tools: ["bash", "write"], faux_responses: [bash(shell(mark)), { text: "не должно прозвучать" }] });
  h.send({ type: "prompt", id: "1", text: "запиши" });
  await h.next("permission");
  await sleep(600);
  assert.equal(existsSync(mark), false);
  h.send({ type: "abort" });
  const end = await h.next("end");
  assert.equal(end.status, "aborted");
  await sleep(300);
  assert.equal(existsSync(mark), false);
  await h.close();
});

test("запись файла требует разрешения", async () => {
  const cwd = tmp();
  const h = harness();
  await init(h, {
    cwd, tools: ["write"],
    faux_responses: [{ tool_calls: [{ name: "write", args: { path: "out.txt", content: "данные" } }] }, { text: "ок" }],
  });
  h.send({ type: "prompt", id: "1", text: "запиши" });
  const perm = await h.next("permission");
  assert.equal(perm.tool, "write");
  assert.equal(existsSync(join(cwd, "out.txt")), false);
  h.send({ type: "permission_result", call_id: perm.call_id, allow: false, reason: "нет" });
  await h.next("end");
  assert.equal(existsSync(join(cwd, "out.txt")), false);
  await h.close();
});

test("инструмент, которого нет в списке, модель вызвать не может", async () => {
  const cwd = tmp();
  const mark = join(cwd, "mark.txt");
  const h = harness();
  await init(h, { cwd, tools: ["read"], faux_responses: [bash(shell(mark)), { text: "ок" }] });
  h.send({ type: "prompt", id: "1", text: "запиши" });
  const end = await h.next("end");
  assert.equal(end.status, "ok");
  assert.equal(existsSync(mark), false);
  assert.ok(!types(h).includes("permission"));
  assert.ok(!toolNames(h.requests[0]).includes("bash"), "bash модели не объявлен");
  await h.close();
});

test("свой инструмент: tool_call и tool_result, без запроса разрешения", async () => {
  const h = harness();
  await init(h, {
    tools: [],
    custom_tools: [{ name: "ping", description: "ответ", parameters: { type: "object", properties: { x: { type: "string" } } } }],
    faux_responses: [{ tool_calls: [{ name: "ping", args: { x: "a" } }] }, { text: "получил" }],
  });
  h.send({ type: "prompt", id: "1", text: "пинг" });
  const call = await h.next("tool_call");
  assert.equal(call.name, "ping");
  assert.deepEqual(call.args, { x: "a" });
  h.send({ type: "tool_result", call_id: call.call_id, ok: true, text: "понг-77" });
  const end = await h.next("end");
  assert.equal(end.status, "ok");
  assert.ok(!types(h).includes("permission"));
  assert.ok(JSON.stringify(h.requests.at(-1).messages).includes("понг-77"));
  await h.close();
});

test("max_turns: ход обрывается статусом max_turns", async () => {
  const h = harness();
  await init(h, {
    tools: ["ls"], max_turns: 2,
    faux_responses: [
      { tool_calls: [{ name: "ls", args: {} }] },
      { tool_calls: [{ name: "ls", args: {} }] },
      { tool_calls: [{ name: "ls", args: {} }] },
      { text: "конец" },
    ],
  });
  h.send({ type: "prompt", id: "1", text: "иди" });
  const timer = setInterval(() => {
    for (const m of h.out) {
      if (m.type === "permission" && !m.__answered) { m.__answered = true; h.send({ type: "permission_result", call_id: m.call_id, allow: true }); }
    }
  }, 20);
  try {
    const end = await h.next("end");
    assert.equal(end.status, "max_turns");
    assert.equal(end.turns, 2, JSON.stringify(h.out.map((m) => [m.type, m.tool || m.status])));
  } finally { clearInterval(timer); }
  await h.close();
});

test("abort во время хода", async () => {
  const h = harness();
  await init(h, { faux_tps: 20, faux_responses: [{ text: "очень длинный ответ ".repeat(60) }] });
  h.send({ type: "prompt", id: "1", text: "говори" });
  await h.next("text_delta");
  h.send({ type: "abort" });
  assert.equal((await h.next("end")).status, "aborted");
  await h.close();
});

test("продолжение из session_file", async () => {
  const dir = tmp();
  const file = join(dir, "s", "history.jsonl");
  const h1 = harness();
  await init(h1, { session_file: file, faux_responses: [{ text: "первый" }] });
  h1.send({ type: "prompt", id: "1", text: "запомни-слово-кактус" });
  await h1.next("end");
  await h1.close();
  assert.ok(existsSync(file));

  const h2 = harness();
  const ready = await init(h2, { session_file: file, faux_responses: [{ text: "второй" }] });
  assert.ok(ready.restored >= 2, `восстановлено ${ready.restored}`);
  h2.send({ type: "prompt", id: "2", text: "что?" });
  await h2.next("end");
  assert.ok(JSON.stringify(h2.requests[0].messages).includes("запомни-слово-кактус"));
  await h2.close();
});

test("изоляция от домашней папки: расширения, скиллы, контекстные файлы и настройки оттуда не читаются", async () => {
  const home = tmp();
  const pi = join(home, ".pi", "agent");
  mkdirSync(join(pi, "skills", "evil"), { recursive: true });
  writeFileSync(join(pi, "skills", "evil", "SKILL.md"), "---\nname: evil\ndescription: ДОМАШНИЙ-СКИЛЛ\n---\nтело");
  writeFileSync(join(pi, "AGENTS.md"), "ДОМАШНИЙ-КОНТЕКСТ");
  writeFileSync(join(pi, "settings.json"), JSON.stringify({ defaultTools: ["bash"] }));
  const cwd = tmp();
  writeFileSync(join(cwd, "AGENTS.md"), "ПРОЕКТНЫЙ-КОНТЕКСТ");
  mkdirSync(join(cwd, ".pi", "skills", "p"), { recursive: true });
  writeFileSync(join(cwd, ".pi", "skills", "p", "SKILL.md"), "---\nname: p\ndescription: ПРОЕКТНЫЙ-СКИЛЛ\n---\nтело");
  const saved = { HOME: process.env.HOME, USERPROFILE: process.env.USERPROFILE, KEY: process.env.OPENAI_API_KEY };
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.OPENAI_API_KEY = "sk-тест";
  try {
    const h = harness();
    const ready = await init(h, { cwd, tools: ["read"], system: "ЧИСТАЯ-ПОДСКАЗКА", faux_responses: [{ text: "ок" }] });
    assert.deepEqual(ready.tools, ["read"]);
    h.send({ type: "prompt", id: "1", text: "привет" });
    await h.next("end");
    const sys = systemOf(h.requests[0]);
    assert.ok(sys.includes("ЧИСТАЯ-ПОДСКАЗКА"));
    for (const bad of ["ДОМАШНИЙ-СКИЛЛ", "ДОМАШНИЙ-КОНТЕКСТ", "ПРОЕКТНЫЙ-КОНТЕКСТ", "ПРОЕКТНЫЙ-СКИЛЛ"]) {
      assert.ok(!sys.includes(bad), `в подсказку попало ${bad}`);
    }
    assert.notEqual(process.env.PI_CODING_AGENT_DIR, join(pi), "служебная папка pi не домашняя");
    assert.ok(!existsSync(join(home, ".pi", "agent", "auth.json")));
    assert.equal(process.env.OPENAI_API_KEY, undefined, "ключи из окружения убраны");
    await h.close();
  } finally {
    for (const [k, v] of Object.entries({ HOME: saved.HOME, USERPROFILE: saved.USERPROFILE, OPENAI_API_KEY: saved.KEY })) {
      if (v === undefined) delete process.env[k]; else process.env[k] = v;
    }
  }
});

test("skills_dir: скиллы берутся только из указанной папки; system_file читается", async () => {
  const skills = tmp();
  mkdirSync(join(skills, "mine"), { recursive: true });
  writeFileSync(join(skills, "mine", "SKILL.md"), "---\nname: mine\ndescription: СВОЙ-СКИЛЛ\n---\nтело");
  const sf = join(tmp(), "system.txt");
  writeFileSync(sf, "ПОДСКАЗКА-ИЗ-ФАЙЛА");
  const h = harness();
  await init(h, { tools: ["read"], system: undefined, system_file: sf, skills_dir: skills, faux_responses: [{ text: "ок" }] });
  h.send({ type: "prompt", id: "1", text: "привет" });
  await h.next("end");
  const sys = systemOf(h.requests[0]);
  assert.ok(sys.includes("ПОДСКАЗКА-ИЗ-ФАЙЛА"));
  assert.ok(sys.includes("СВОЙ-СКИЛЛ"));
  await h.close();
});

test("второй prompt во время хода: busy; без init: ошибка", async () => {
  const h = harness();
  h.send({ type: "prompt", id: "0", text: "а" });
  assert.match((await h.next("error")).message, /нет init/);
  await init(h, { faux_tps: 20, faux_responses: [{ text: "долго ".repeat(80) }] });
  h.send({ type: "prompt", id: "1", text: "а" });
  await h.next("text_delta");
  h.send({ type: "prompt", id: "2", text: "б" });
  assert.match((await h.next("error")).message, /busy/);
  h.send({ type: "abort" });
  await h.next("end");
  await h.close();
});
