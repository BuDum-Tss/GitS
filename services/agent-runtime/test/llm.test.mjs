// Тесты llm.mjs на поддельном поставщике: без сети и без входа.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";
import { after, test } from "node:test";
import { createLlm } from "../llm.mjs";

const dirs = [];
after(() => dirs.forEach((d) => rmSync(d, { recursive: true, force: true })));

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
  const llm = createLlm({ input, output });
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
  const close = async () => { send({ type: "shutdown" }); await llm.done; };
  return { send, out, next, close };
}

const init = (h, responses = [], extra = {}) => {
  h.send({ type: "init", provider: "faux", auth_file: "нет.json", faux_responses: responses, ...extra });
  return h.next("ready");
};
const ask = (h, id, extra = {}) =>
  h.send({ type: "complete", id, model: "faux", thinking: "minimal", system: "сис", user: `вопрос ${id}`, image_b64: null, image_mime: "image/jpeg", stream: false, ...extra });

test("текст целиком, без дельт", async () => {
  const h = harness();
  await init(h, [{ text: "Привет." }]);
  ask(h, "a");
  const d = await h.next("done", (m) => m.id === "a");
  assert.equal(d.text, "Привет.");
  assert.equal(typeof d.usage.input, "number");
  assert.ok(!h.out.some((m) => m.type === "delta"));
  await h.close();
});

test("поток: дельты, затем один done", async () => {
  const h = harness();
  await init(h, [{ text: "Один два три четыре пять." }]);
  ask(h, "s", { stream: true });
  const d = await h.next("done", (m) => m.id === "s");
  const deltas = h.out.filter((m) => m.type === "delta" && m.id === "s");
  assert.ok(deltas.length >= 1);
  assert.equal(deltas.map((m) => m.text).join(""), d.text);
  assert.equal(h.out.filter((m) => m.type === "done").length, 1);
  await h.close();
});

test("два параллельных запроса", async () => {
  const h = harness();
  await init(h, [{ echo: true }, { echo: true }]);
  ask(h, "p1");
  ask(h, "p2");
  const [a, b] = await Promise.all([h.next("done", (m) => m.id === "p1"), h.next("done", (m) => m.id === "p2")]);
  assert.match(a.text, /^вопрос p1\|/);
  assert.match(b.text, /^вопрос p2\|/);
  await h.close();
});

test("отмена: failed cancelled, процесс жив", async () => {
  const h = harness();
  await init(h, [{ text: "слово ".repeat(400) }, { text: "Живой." }], { faux_tps: 20 });
  ask(h, "c", { stream: true });
  await h.next("delta", (m) => m.id === "c");
  h.send({ type: "cancel", id: "c" });
  const f = await h.next("failed", (m) => m.id === "c");
  assert.equal(f.error, "cancelled");
  ask(h, "after");
  assert.equal((await h.next("done", (m) => m.id === "after")).text, "Живой.");
  await h.close();
});

test("картинка доходит блоком изображения", async () => {
  const h = harness();
  await init(h, [{ echo: true }]);
  ask(h, "i", { image_b64: "QUJD", image_mime: "image/png", user: "что тут?" });
  const d = await h.next("done", (m) => m.id === "i");
  assert.match(d.text, /что тут\?\|image:image\/png:4,text\|sys=сис/);
  await h.close();
});

test("ошибка поставщика даёт failed, процесс жив", async () => {
  const h = harness();
  await init(h, [{ error: "лимит исчерпан" }, { text: "Снова жива." }]);
  ask(h, "e");
  const f = await h.next("failed", (m) => m.id === "e");
  assert.match(f.error, /лимит исчерпан/);
  ask(h, "ok");
  assert.equal((await h.next("done", (m) => m.id === "ok")).text, "Снова жива.");
  await h.close();
});

test("нет входа: фатальная ошибка", async () => {
  const d = mkdtempSync(join(tmpdir(), "agent-llm-")); dirs.push(d);
  const h = harness();
  h.send({ type: "init", provider: "openai", auth_file: join(d, "auth.json") });
  const err = await h.next("error");
  assert.equal(err.fatal, true);
  assert.match(err.message, /нет входа/);
  await h.close();
});

test("запрос до init: failed", async () => {
  const h = harness();
  ask(h, "x");
  assert.equal((await h.next("failed")).error, "нет init");
  await h.close();
});
