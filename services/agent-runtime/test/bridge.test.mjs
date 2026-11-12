// Тесты моста на поддельном поставщике: без сети и без входа.
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";
import { after, test } from "node:test";
import { compactMessages, createBridge } from "../bridge.mjs";

const dirs = [];
const tmp = () => { const d = mkdtempSync(join(tmpdir(), "agent-runtime-")); dirs.push(d); return d; };
after(() => dirs.forEach((d) => rmSync(d, { recursive: true, force: true })));

const TOOLS = [{
  name: "call_tool", description: "вызвать инструмент",
  parameters: { type: "object", properties: { tool_id: { type: "string" } }, required: ["tool_id"] },
}];

/** Мост на потоках: send пишет строку ядра, next ждёт ещё не разобранное сообщение моста. */
function harness() {
  const input = new PassThrough();
  const output = new PassThrough();
  const out = [];
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
  const bridge = createBridge({ input, output, log: () => {} });
  const send = (obj) => input.write(JSON.stringify(obj) + "\n");
  const next = (type, extra = () => true, ms = 5000) => {
    const pred = (m) => m.type === type && extra(m);
    const seen = out.find((m) => !m.__taken && pred(m));
    if (seen) { seen.__taken = true; return Promise.resolve(seen); }
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error(`не дождались ${type}; получено: ${JSON.stringify(out)}`)), ms);
      waiters.push({ pred, resolve: (m) => { clearTimeout(t); resolve(m); } });
    });
  };
  const close = async () => { send({ type: "shutdown" }); await bridge.done; };
  return { send, out, next, close };
}

const init = (h, extra = {}) => {
  h.send({
    type: "init", provider: "faux", model: "faux", thinking: "off", system: "системная подсказка", tools: TOOLS,
    auth_file: "нет.json", session_file: null, session_id: "t", max_context_chars: 0, faux_responses: [], ...extra,
  });
  return h.next("ready");
};
const userTexts = (messages) => messages.filter((m) => m.role === "user")
  .map((m) => (typeof m.content === "string" ? m.content : m.content.map((c) => c.text).join("")));
const readSaved = (file) => JSON.parse(readFileSync(file, "utf-8"));

test("рукопожатие: ready и ошибка без входа", async () => {
  const h = harness();
  const ready = await init(h);
  assert.equal(ready.restored, 0);
  await h.close();

  const h2 = harness();
  h2.send({ type: "init", provider: "openai", model: "gpt-6-luna", system: "", tools: [], auth_file: join(tmp(), "auth.json"), session_file: null, session_id: "x" });
  const err = await h2.next("error");
  assert.equal(err.fatal, true);
  assert.match(err.message, /нет входа/);
  await h2.close();
});

test("текст и end", async () => {
  const h = harness();
  await init(h, { faux_responses: [{ text: "Привет." }] });
  h.send({ type: "prompt", id: "t1", text: "скажи привет" });
  const text = await h.next("text");
  assert.equal(text.id, "t1");
  assert.equal(text.text, "Привет.");
  const end = await h.next("end");
  assert.equal(end.id, "t1");
  assert.equal(end.status, "ok");
  assert.equal(end.error, null);
  assert.equal(end.usage.requests, 1);
  assert.ok(h.out.some((m) => m.type === "text_delta"));
  await h.close();
});

test("вызов инструмента: ok и ошибка", async () => {
  const file = join(tmp(), "s.json");
  const h = harness();
  await init(h, {
    session_file: file,
    faux_responses: [
      { text: "Смотрю.", tool_calls: [{ name: "call_tool", args: { tool_id: "a" } }] },
      { tool_calls: [{ name: "call_tool", args: { tool_id: "b" } }] },
      { text: "Готово." },
    ],
  });
  h.send({ type: "prompt", id: "t1", text: "работай" });
  const c1 = await h.next("tool_call");
  assert.equal(c1.name, "call_tool");
  assert.deepEqual(c1.args, { tool_id: "a" });
  assert.equal(c1.id, "t1");
  h.send({ type: "tool_result", call_id: c1.call_id, ok: true, text: "результат А" });
  const c2 = await h.next("tool_call");
  h.send({ type: "tool_result", call_id: c2.call_id, ok: false, text: "сломалось" });
  const end = await h.next("end");
  assert.equal(end.status, "ok");
  assert.equal(end.usage.requests, 3);
  const results = readSaved(file).filter((m) => m.role === "toolResult");
  assert.equal(results.length, 2);
  assert.ok(!results[0].isError);
  assert.equal(results[0].content[0].text, "результат А");
  assert.equal(results[1].isError, true);
  assert.match(results[1].content[0].text, /сломалось/);
  await h.close();
});

test("steer во время инструмента виден модели на следующем шаге", async () => {
  const file = join(tmp(), "s.json");
  const h = harness();
  await init(h, {
    session_file: file,
    faux_responses: [
      { tool_calls: [{ name: "call_tool", args: { tool_id: "a" } }] },
      { text: "Учла." },
    ],
  });
  h.send({ type: "prompt", id: "t1", text: "рабочий цикл" });
  const c = await h.next("tool_call");
  h.send({ type: "steer", text: "Пользователь говорит: привет" });
  h.send({ type: "tool_result", call_id: c.call_id, ok: true, text: "ок" });
  assert.equal((await h.next("end")).status, "ok");
  const saved = readSaved(file);
  const roles = saved.filter((m) => m.role !== "system").map((m) => m.role);
  // подмешанное сообщение стоит после результата инструмента и до последнего ответа модели
  assert.deepEqual(roles, ["user", "assistant", "toolResult", "user", "assistant"]);
  assert.ok(userTexts(saved).includes("Пользователь говорит: привет"));
  await h.close();
});

test("abort во время инструмента: aborted, следующий prompt с той же историей", async () => {
  const file = join(tmp(), "s.json");
  const h = harness();
  await init(h, {
    session_file: file,
    faux_responses: [
      { text: "Иду.", tool_calls: [{ name: "call_tool", args: { tool_id: "long" } }] },
      { text: "Налёт, ставлю паузу." },
    ],
  });
  h.send({ type: "prompt", id: "t1", text: "рабочий цикл" });
  await h.next("tool_call");
  h.send({ type: "abort" });
  const end = await h.next("end");
  assert.equal(end.id, "t1");
  assert.equal(end.status, "aborted");
  h.send({ type: "prompt", id: "t2", text: "срочно: налёт" });
  const end2 = await h.next("end");
  assert.equal(end2.id, "t2");
  assert.equal(end2.status, "ok");
  assert.deepEqual(userTexts(readSaved(file)), ["рабочий цикл", "срочно: налёт"]);
  assert.equal(h.out.filter((m) => m.type === "end").length, 2);
  await h.close();
});

test("история сохраняется и восстанавливается; reset удаляет файл", async () => {
  const file = join(tmp(), "s.json");
  const h = harness();
  await init(h, { session_file: file, faux_responses: [{ text: "Один." }] });
  h.send({ type: "prompt", id: "t1", text: "первый" });
  await h.next("end");
  assert.ok(existsSync(file));
  assert.ok(!existsSync(file + ".tmp"));
  await h.close();

  const h2 = harness();
  const ready = await init(h2, { session_file: file, faux_responses: [{ text: "Два." }] });
  assert.equal(ready.restored, 2); // user и assistant, без системного
  h2.send({ type: "prompt", id: "t2", text: "второй" });
  await h2.next("end");
  assert.deepEqual(userTexts(readSaved(file)), ["первый", "второй"]);

  h2.send({ type: "reset" });
  await h2.next("reset_done");
  assert.ok(!existsSync(file));
  await h2.close();
});

test("set_system меняет подсказку, история остаётся", async () => {
  const file = join(tmp(), "s.json");
  const h = harness();
  await init(h, { session_file: file, faux_responses: [{ text: "Один." }, { text: "Два." }] });
  h.send({ type: "prompt", id: "t1", text: "первый" });
  await h.next("end");
  h.send({ type: "set_system", system: "новая подсказка" });
  h.send({ type: "prompt", id: "t2", text: "второй" });
  await h.next("end");
  const saved = readSaved(file);
  assert.deepEqual(userTexts(saved), ["первый", "второй"]);
  assert.ok(JSON.stringify(saved).includes("новая подсказка"));
  await h.close();
});

test("сжатие: старое отбрасывается по границе user, пара вызов/результат цела", () => {
  const u = (t) => ({ role: "user", content: t, timestamp: 1 });
  const a = (t) => ({ role: "assistant", content: [{ type: "text", text: t }] });
  const tr = (t) => ({ role: "toolResult", toolCallId: "x", content: [{ type: "text", text: t }] });
  const filler = "я".repeat(400);
  const msgs = [{ role: "system", content: "sys" }, u("1" + filler), a("a" + filler), tr("t" + filler), a("b" + filler), u("2" + filler), a("c" + filler), u("3"), a("d")];
  assert.equal(compactMessages(msgs, 100000), msgs);
  const out = compactMessages(msgs, 1200);
  assert.equal(out[0].role, "system");
  assert.equal(out[1].content, "[ранняя часть разговора опущена]");
  assert.equal(out[2].role, "user");
  assert.ok(out.length < msgs.length);
  assert.ok(out.slice(2).every((m, i, arr) => m.role !== "toolResult" || arr[i - 1]?.role === "assistant"));
});

test("сжатие работает в мосте: ходы проходят при малом лимите", async () => {
  const h = harness();
  await init(h, { max_context_chars: 300, faux_responses: [{ text: "А" }, { text: "Б" }, { text: "В" }] });
  for (const id of ["1", "2", "3"]) {
    h.send({ type: "prompt", id, text: "длинное сообщение ".repeat(20) });
    assert.equal((await h.next("end", (m) => m.id === id)).status, "ok");
  }
  await h.close();
});

test("busy и idle", async () => {
  const h = harness();
  await init(h, { faux_responses: [{ tool_calls: [{ name: "call_tool", args: { tool_id: "a" } }] }, { text: "ок" }] });
  h.send({ type: "steer", text: "рано" });
  assert.equal((await h.next("error")).message, "idle");
  h.send({ type: "follow_up", text: "рано" });
  assert.equal((await h.next("error")).message, "idle");
  h.send({ type: "prompt", id: "t1", text: "раз" });
  const c = await h.next("tool_call");
  h.send({ type: "prompt", id: "t2", text: "два" });
  const busy = await h.next("error");
  assert.equal(busy.message, "busy");
  assert.equal(busy.id, "t2");
  assert.equal(busy.fatal, false);
  h.send({ type: "tool_result", call_id: c.call_id, ok: true, text: "ок" });
  assert.equal((await h.next("end")).id, "t1");
  assert.equal(h.out.filter((m) => m.type === "end").length, 1);
  await h.close();
});

test("битый session_file: пустая история, не падает", async () => {
  const file = join(tmp(), "s.json");
  writeFileSync(file, "{не json");
  const h = harness();
  const ready = await init(h, { session_file: file, faux_responses: [{ text: "Привет." }] });
  assert.equal(ready.restored, 0);
  h.send({ type: "prompt", id: "t1", text: "раз" });
  assert.equal((await h.next("end")).status, "ok");
  assert.equal(readSaved(file).filter((m) => m.role === "user").length, 1);
  await h.close();
});

test("ошибка поставщика даёт end со статусом error, мост жив", async () => {
  const h = harness();
  await init(h, { faux_responses: [] }); // ответы кончились: поставщик вернёт ошибку
  h.send({ type: "prompt", id: "t1", text: "раз" });
  const end = await h.next("end");
  assert.equal(end.status, "error");
  assert.ok(end.error);
  await h.close();
});
