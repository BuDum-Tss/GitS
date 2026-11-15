// Общий скрипт сайта монтажа (без библиотек): переключатель темы, кнопки «на весь экран», попап предпросмотра компонентов.
(function () {
  const root = document.documentElement;
  const toggle = document.getElementById('theme-toggle');
  if (toggle) {
    toggle.onclick = () => {
      const dark = !root.classList.contains('dark');
      root.classList.toggle('dark', dark);
      try { localStorage.setItem('montage-theme', dark ? 'dark' : 'light'); } catch (e) { /* без запоминания */ }
    };
  }
  // data-fullscreen='#селектор': Fullscreen API для элемента; Esc выходит штатно
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-fullscreen]');
    if (!button) return;
    const target = document.querySelector(button.dataset.fullscreen);
    if (!target) return;
    if (document.fullscreenElement) document.exitFullscreen();
    else if (target.requestFullscreen) target.requestFullscreen().catch(() => {});
  });
  // data-preview='адрес': попап <dialog> с iframe предпросмотра; Esc закрывает, клик по фону и кнопка закрывают
  const dialog = document.getElementById('preview-dialog');
  if (dialog) {
    const frame = document.getElementById('preview-frame');
    const title = document.getElementById('preview-title');
    dialog.addEventListener('close', () => { frame.src = 'about:blank'; });
    dialog.addEventListener('click', (event) => {
      if (event.target === dialog || event.target.closest('[data-close]')) dialog.close();
    });
    document.addEventListener('click', (event) => {
      const button = event.target.closest('[data-preview]');
      if (!button) return;
      title.textContent = button.dataset.title || 'Предпросмотр';
      frame.src = button.dataset.preview;
      dialog.showModal();
    });
  }
})();
