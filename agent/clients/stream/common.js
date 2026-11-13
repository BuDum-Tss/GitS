// Общая часть сцен стрима: стикер и субтитр по presence ядра (/api/state, тот же адрес, что у дашборда).
(function () {
  const POLL_MS = 800;
  function bind({sticker, sub, hold = 4, perChar = 0.07}) {
    let current = '';
    async function tick() {
      try {
        const d = await (await fetch('/api/state', {cache: 'no-store'})).json();
        const p = d.presence || {};
        const name = p.sticker || 'idle';
        if (sticker && name !== current) { current = name; sticker.src = `/static/stream/stickers/${name}.png`; }
        if (sticker) sticker.classList.toggle('speaking', !!p.speaking);
        if (sub) {
          const text = (p.last_say || '').trim(), age = Date.now() / 1000 - (p.last_say_ts || 0);
          const show = !!text && (p.speaking || age < hold + text.length * perChar);
          if (text && sub.dataset.text !== text) { sub.dataset.text = text; sub.textContent = text; }
          sub.classList.toggle('on', show);
        }
      } catch (e) { /* ядро недоступно: оставляем как есть */ }
    }
    tick(); setInterval(tick, POLL_MS);
  }
  window.StreamScene = {bind};
})();
