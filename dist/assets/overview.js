(() => {
  'use strict';
  const chapters = [...document.querySelectorAll('.chapter')];
  const navigation = [...document.querySelectorAll('[data-chapter]')];
  const labels = ['文化新生', '平台协同', '感知与动作', '具身交互', '开放共创', '走向服务'];
  let active = 0;
  function show(index, updateHash = true) {
    active = Math.max(0, Math.min(chapters.length - 1, index));
    chapters.forEach((chapter, i) => { chapter.hidden = i !== active; });
    navigation.forEach((button, i) => {
      if (i === active) button.setAttribute('aria-current', 'step');
      else button.removeAttribute('aria-current');
    });
    document.getElementById('previous').disabled = active === 0;
    document.getElementById('next').disabled = active === chapters.length - 1;
    document.getElementById('page-count').innerHTML = `${String(active + 1).padStart(2, '0')} <i>/ 06</i>`;
    document.getElementById('chapter-label').textContent = labels[active];
    document.getElementById('display-status').textContent = `第 ${active + 1} 章，共 6 章：${labels[active]}`;
    document.title = `${labels[active]} · 项目全景 · 岐黄巧手`;
    if (updateHash) history.pushState(null, '', '#' + chapters[active].id);
    window.scrollTo({top: 0, behavior: 'instant'});
  }
  function fromHash() {
    const index = chapters.findIndex(chapter => '#' + chapter.id === location.hash);
    show(index < 0 ? 0 : index, false);
  }
  navigation.forEach((button, index) => button.addEventListener('click', () => show(index)));
  document.querySelectorAll('[data-go]').forEach(button => button.addEventListener('click', () => show(chapters.findIndex(chapter => chapter.id === button.dataset.go))));
  document.getElementById('previous').addEventListener('click', () => show(active - 1));
  document.getElementById('next').addEventListener('click', () => show(active + 1));
  document.addEventListener('keydown', event => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.target.closest('input,textarea,select,[contenteditable="true"]')) return;
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
      event.preventDefault();
      show(active + (event.key === 'ArrowRight' ? 1 : -1));
      navigation[active].focus({preventScroll: true});
    }
  });
  window.addEventListener('hashchange', fromHash);
  const fullscreen = document.getElementById('fullscreen');
  if (!document.fullscreenEnabled) fullscreen.hidden = true;
  fullscreen.addEventListener('click', async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch (_) {
      document.getElementById('display-status').textContent = '当前浏览器未能进入全屏，可以继续使用章节展示。';
      fullscreen.textContent = '全屏暂不可用';
    }
  });
  document.addEventListener('fullscreenchange', () => { fullscreen.textContent = document.fullscreenElement ? '退出全屏 ⛶' : '全屏展示 ⛶'; });
  fromHash();
})();
