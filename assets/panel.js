/* ============================================================
   抖店多店铺控制面板 · 前端逻辑
   纯原生 JS (无框架)，通过 SSE 接收实时日志与进度
   组织:
     1. 工具函数
     2. 全局状态
     3. 危险确认弹窗
     4. 渲染 (任务卡片 / 进度 / 日志)
     5. 控制区交互 (店铺 / 模式 / 运行)
     6. SSE 实时通信
     7. 初始化
   ============================================================ */
(function () {
  'use strict';

  const $ = s => document.querySelector(s);
  const tasks = new Map();          // task_id -> 最新 brief
  let MAXP = 2;

  /* ---------- 业务工作台导航 / 盖亚前端阶段 ---------- */
  function initWorkspaceNavigation() {
    const tabs = document.querySelectorAll('.workspace-tab');
    const views = document.querySelectorAll('[data-view-panel]');
    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        const target = tab.dataset.view;
        tabs.forEach(x => x.classList.toggle('active', x === tab));
        views.forEach(view => view.classList.toggle('active', view.dataset.viewPanel === target));
        try { localStorage.setItem('doudian-panel-view', target); } catch (e) {}
      });
    });
    let saved = 'gaia';
    try { saved = localStorage.getItem('doudian-panel-view') || 'gaia'; } catch (e) {}
    const initial = document.querySelector('.workspace-tab[data-view="' + saved + '"]');
    if (initial) initial.click();
  }

  function initMascotExperience() {
    const stage = $('#opsStage');
    const art = $('#mascotArt');
    const forwardLayer = $('#characterForwardLayer');
    const idleLayer = $('#characterIdleLayer');
    const greetingNote = $('#mascotGreetingNote');
    const greetHotspot = $('#mascotGreetHotspot');
    const boxHotspot = $('#mascotBoxHotspot');
    const eyeMasks = Array.from(art?.querySelectorAll('.character-eye-mask') || []);
    const irisLayers = eyeMasks.map(mask => mask.querySelector('img')).filter(Boolean);
    const boxVideo = $('#characterBoxLayer');
    const fullActionVideo = $('#characterFullActionLayer');
    const waveVideo = $('#characterWaveLayer');
    if (!stage || !art || !forwardLayer || !idleLayer || !greetHotspot || !boxHotspot || !boxVideo || !fullActionVideo || !waveVideo || irisLayers.length !== 2) return;

    const STATES = Object.freeze({
      LOADING: 'loading',
      FALLBACK: 'fallback',
      IDLE: 'idle',
      OPENING: 'opening',
      OPENED: 'opened',
      GREETING_FULL: 'greetingFull',
      GREETING_OPENED: 'greetingOpened',
      RETURNING: 'returning'
    });
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const coarsePointer = window.matchMedia('(pointer: coarse)').matches;
    const finePointer = window.matchMedia('(hover: hover) and (pointer: fine)').matches;
    const controller = new AbortController();
    const listenerOptions = {signal: controller.signal};
    const videos = [boxVideo, fullActionVideo, waveVideo];
    let characterState = STATES.LOADING;
    let resourcesReady = false;
    let gazeListenersEnabled = false;
    let hoverTimer = 0;
    let returnTimer = 0;
    let reducedGreetingTimer = 0;
    let boxPointerInside = false;
    let transitionIntent = 'none';
    let eyeTargetX = 0;
    let eyeTargetY = 0;
    let eyeCurrentX = 0;
    let eyeCurrentY = 0;
    let eyeFrame = 0;
    let eyeReturnTimer = 0;
    let hasPointerMoved = false;
    let pointerX = 0;
    let pointerY = 0;
    let eyeCenterX = 0;
    let eyeCenterY = 0;
    let maxEyeX = 0;
    let maxEyeY = 0;

    function clearTimer(timer) {
      if (timer) window.clearTimeout(timer);
      return 0;
    }

    function refreshEyeGeometry() {
      const maskBounds = eyeMasks.map(mask => mask.getBoundingClientRect());
      const irisBoxDiameter = Math.max(...irisLayers.map(iris => iris.getBoundingClientRect().width));
      // The square WebP contains transparent breathing room; collision math uses
      // the visible iris, while the full 32px canvas stays aligned with the
      // pre-composited forward-facing fallback image.
      const irisDiameter = irisBoxDiameter * .80;
      eyeCenterX = maskBounds.reduce((total, bounds) => total + bounds.left + bounds.width / 2, 0) / maskBounds.length;
      eyeCenterY = maskBounds.reduce((total, bounds) => total + bounds.top + bounds.height / 2, 0) / maskBounds.length;
      const safeInsetX = Math.max(2, Math.min(...maskBounds.map(bounds => bounds.width)) * .045);
      const safeInsetY = Math.max(3, Math.min(...maskBounds.map(bounds => bounds.height)) * .085);
      const maxAvailableX = Math.min(...maskBounds.map(bounds => Math.max(1, (bounds.width - irisDiameter) / 2 - safeInsetX)));
      const maxAvailableY = Math.min(...maskBounds.map(bounds => Math.max(1, (bounds.height - irisDiameter) / 2 - safeInsetY)));
      maxEyeX = maxAvailableX * .70;
      maxEyeY = maxAvailableY * .60;
      requestEyeFrame();
    }

    function canTrackEyes() {
      return !reducedMotion
        && !coarsePointer
        && finePointer
        && document.hasFocus()
        && document.visibilityState !== 'hidden'
        && resourcesReady
        && characterState === STATES.IDLE
        && transitionIntent === 'none';
    }

    function calculateEyeTarget() {
      if (!canTrackEyes() || !hasPointerMoved || !maxEyeX || !maxEyeY) {
        eyeTargetX = 0;
        eyeTargetY = 0;
        return;
      }
      const dx = pointerX - eyeCenterX;
      const dy = pointerY - eyeCenterY;
      const angle = Math.atan2(dy, dx);
      const distance = Math.hypot(dx, dy);
      const referenceDistance = Math.max(1, Math.min(window.innerWidth, window.innerHeight) * .42);
      const strength = Math.min(distance / referenceDistance, 1);
      const rawX = Math.cos(angle) * maxEyeX * strength;
      const rawY = Math.sin(angle) * maxEyeY * strength;
      let targetX = rawX;
      let targetY = rawY;
      const normalized = (targetX * targetX) / (maxEyeX * maxEyeX)
        + (targetY * targetY) / (maxEyeY * maxEyeY);
      if (normalized > 1) {
        const scale = 1 / Math.sqrt(normalized);
        targetX *= scale;
        targetY *= scale;
      }
      eyeTargetX = targetX;
      eyeTargetY = targetY;
    }

    function drawEyes() {
      eyeFrame = 0;
      if (characterState !== STATES.IDLE) return;

      calculateEyeTarget();
      const easing = .11;
      eyeCurrentX += (eyeTargetX - eyeCurrentX) * easing;
      eyeCurrentY += (eyeTargetY - eyeCurrentY) * easing;
      irisLayers.forEach(iris => {
        iris.style.setProperty('--gaze-x', eyeCurrentX.toFixed(2) + 'px');
        iris.style.setProperty('--gaze-y', eyeCurrentY.toFixed(2) + 'px');
      });
      if (Math.abs(eyeTargetX - eyeCurrentX) > .03 || Math.abs(eyeTargetY - eyeCurrentY) > .03) {
        eyeFrame = window.requestAnimationFrame(drawEyes);
      }
    }

    function requestEyeFrame() {
      if (!eyeFrame) eyeFrame = window.requestAnimationFrame(drawEyes);
    }

    function requestEyesToCenter() {
      eyeTargetX = 0;
      eyeTargetY = 0;
      requestEyeFrame();
    }

    function clearPointerGaze() {
      hasPointerMoved = false;
      requestEyesToCenter();
    }

    function resetGaze() {
      clearPointerGaze();
    }

    function handleVisibilityChange() {
      if (document.visibilityState === 'hidden') resetGaze();
    }

    function handleGlobalPointerMove(event) {
      pointerX = event.clientX;
      pointerY = event.clientY;
      hasPointerMoved = true;
      requestEyeFrame();
    }

    function centerEyesImmediately() {
      if (eyeFrame) window.cancelAnimationFrame(eyeFrame);
      eyeFrame = 0;
      eyeTargetX = 0;
      eyeTargetY = 0;
      eyeCurrentX = 0;
      eyeCurrentY = 0;
      irisLayers.forEach(iris => {
        iris.style.setProperty('--gaze-x', '0px');
        iris.style.setProperty('--gaze-y', '0px');
      });
    }

    function prepareEyesForVideo() {
      requestEyesToCenter();
      if (reducedMotion || coarsePointer || !finePointer) return Promise.resolve();
      return new Promise(resolve => {
        eyeReturnTimer = window.setTimeout(() => {
          eyeReturnTimer = 0;
          resolve();
        }, 120);
      });
    }

    function pauseAndReset(video) {
      video.pause();
      try { video.currentTime = 0; } catch (error) {}
    }

    function setState(nextState) {
      if (nextState !== STATES.IDLE) centerEyesImmediately();
      characterState = nextState;
      art.dataset.characterState = nextState;
      const boxIsOpen = nextState === STATES.OPENING || nextState === STATES.OPENED || nextState === STATES.GREETING_OPENED;
      const greeting = nextState === STATES.GREETING_FULL || nextState === STATES.GREETING_OPENED;
      boxHotspot.setAttribute('aria-pressed', String(boxIsOpen));
      greetingNote?.classList.toggle('is-visible', greeting);
      if (nextState === STATES.IDLE) requestEyeFrame();
    }

    function waitUntilPlayable(video) {
      if (video.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) return Promise.resolve(true);
      if (video.error) return Promise.resolve(false);
      return new Promise(resolve => {
        const onReady = () => resolve(true);
        const onError = () => resolve(false);
        video.addEventListener('canplay', onReady, {once: true, signal: controller.signal});
        video.addEventListener('error', onError, {once: true, signal: controller.signal});
        video.load();
      });
    }

    async function waitForImage(image) {
      if (!image.complete) {
        const loaded = await new Promise(resolve => {
          image.addEventListener('load', () => resolve(true), {once: true, signal: controller.signal});
          image.addEventListener('error', () => resolve(false), {once: true, signal: controller.signal});
        });
        if (!loaded) return false;
      }
      if (!image.naturalWidth) return false;
      if (typeof image.decode === 'function') {
        try { await image.decode(); } catch (error) { return false; }
      }
      return true;
    }

    function enableGlobalGaze() {
      if (gazeListenersEnabled || reducedMotion || coarsePointer || !finePointer) return;
      gazeListenersEnabled = true;
      window.addEventListener('pointermove', handleGlobalPointerMove, {signal: controller.signal, passive: true});
      document.documentElement.addEventListener('pointerleave', resetGaze, listenerOptions);
      window.addEventListener('blur', resetGaze, listenerOptions);
      document.addEventListener('visibilitychange', handleVisibilityChange, listenerOptions);
    }

    async function initializeCharacterResources() {
      setState(STATES.LOADING);
      const imageResults = await Promise.all([
        waitForImage(forwardLayer),
        waitForImage(idleLayer),
        ...irisLayers.map(waitForImage)
      ]);
      const videoResults = await Promise.all(videos.map(waitUntilPlayable));
      if (controller.signal.aborted) return;
      if (!imageResults.every(Boolean) || !videoResults.every(Boolean)) {
        resourcesReady = false;
        setState(STATES.FALLBACK);
        return;
      }
      await new Promise(resolve => window.requestAnimationFrame(resolve));
      if (controller.signal.aborted) return;
      centerEyesImmediately();
      refreshEyeGeometry();
      resourcesReady = true;
      setState(STATES.IDLE);
      enableGlobalGaze();
    }

    async function playVideo(video, nextState, shouldStart = () => true) {
      if (!resourcesReady) return false;
      const ready = await waitUntilPlayable(video);
      if (!ready || controller.signal.aborted || !shouldStart()) return false;
      if (characterState === STATES.IDLE || characterState === STATES.RETURNING) {
        await prepareEyesForVideo();
      }
      if (controller.signal.aborted || !shouldStart()) return false;
      videos.forEach(candidate => {
        if (candidate !== video) pauseAndReset(candidate);
      });
      pauseAndReset(video);
      setState(nextState);
      try {
        await video.play();
        return true;
      } catch (error) {
        returnToIdle();
        return false;
      }
    }

    function returnToIdle() {
      if (!resourcesReady) {
        setState(STATES.FALLBACK);
        return;
      }
      hoverTimer = clearTimer(hoverTimer);
      returnTimer = clearTimer(returnTimer);
      transitionIntent = 'none';
      videos.forEach(pauseAndReset);
      setState(STATES.RETURNING);
      returnTimer = window.setTimeout(() => {
        returnTimer = 0;
        if (characterState !== STATES.RETURNING) return;
        refreshEyeGeometry();
        setState(STATES.IDLE);
        if (boxPointerInside && !coarsePointer) requestBoxOpening();
      }, 120);
    }

    async function beginBoxOpening() {
      if (!resourcesReady || !boxPointerInside || characterState !== STATES.IDLE || transitionIntent !== 'none') return;
      if (reducedMotion) return;
      transitionIntent = 'boxOpening';
      await playVideo(boxVideo, STATES.OPENING, () => boxPointerInside && characterState === STATES.IDLE && transitionIntent === 'boxOpening');
      if (transitionIntent === 'boxOpening') transitionIntent = 'none';
    }

    function requestBoxOpening() {
      hoverTimer = clearTimer(hoverTimer);
      boxPointerInside = true;
      if (!resourcesReady || characterState !== STATES.IDLE) return;
      hoverTimer = window.setTimeout(() => {
        hoverTimer = 0;
        beginBoxOpening();
      }, 120);
    }

    function beginBoxClosing() {
      boxPointerInside = false;
      hoverTimer = clearTimer(hoverTimer);
      if (transitionIntent === 'boxOpening') {
        transitionIntent = 'none';
        requestEyeFrame();
        return;
      }
      if (transitionIntent === 'greetAfterOpen' || characterState === STATES.GREETING_FULL || characterState === STATES.GREETING_OPENED) return;
      if (characterState === STATES.OPENING || characterState === STATES.OPENED) returnToIdle();
    }

    function showReducedGreeting() {
      reducedGreetingTimer = clearTimer(reducedGreetingTimer);
      transitionIntent = 'reducedGreeting';
      greetingNote?.classList.add('is-visible');
      reducedGreetingTimer = window.setTimeout(() => {
        reducedGreetingTimer = 0;
        transitionIntent = 'none';
        greetingNote?.classList.remove('is-visible');
      }, 2500);
    }

    async function startGreetingFull() {
      if (characterState !== STATES.IDLE || transitionIntent !== 'none') return;
      transitionIntent = 'greetFull';
      if (reducedMotion) {
        showReducedGreeting();
        return;
      }
      await playVideo(fullActionVideo, STATES.GREETING_FULL, () => characterState === STATES.IDLE && transitionIntent === 'greetFull');
      transitionIntent = 'none';
    }

    async function startGreetingOpened() {
      if (characterState !== STATES.OPENED && characterState !== STATES.OPENING) return;
      transitionIntent = 'greetAfterOpen';
      if (reducedMotion) {
        showReducedGreeting();
        return;
      }
      await playVideo(waveVideo, STATES.GREETING_OPENED, () => characterState === STATES.OPENED || characterState === STATES.OPENING);
      transitionIntent = 'none';
    }

    function greetMikey(event) {
      event.stopPropagation();
      hoverTimer = clearTimer(hoverTimer);
      if (!resourcesReady) return;
      if (transitionIntent === 'boxOpening' && characterState === STATES.IDLE) {
        transitionIntent = 'none';
        requestEyesToCenter();
        startGreetingFull();
        return;
      }
      if (characterState === STATES.GREETING_FULL || characterState === STATES.GREETING_OPENED || characterState === STATES.RETURNING || transitionIntent !== 'none') return;
      if (characterState === STATES.OPENING) {
        transitionIntent = 'greetAfterOpen';
        return;
      }
      if (characterState === STATES.OPENED) {
        startGreetingOpened();
        return;
      }
      startGreetingFull();
    }

    function handleBoxEnded() {
      if (characterState !== STATES.OPENING) return;
      boxVideo.pause();
      try { boxVideo.currentTime = Math.max(0, boxVideo.duration - .04); } catch (error) {}
      setState(STATES.OPENED);
      if (transitionIntent === 'greetAfterOpen') startGreetingOpened();
      else if (!boxPointerInside) returnToIdle();
    }

    function handleFullActionEnded() {
      if (characterState === STATES.GREETING_FULL) returnToIdle();
    }

    function handleWaveEnded() {
      if (characterState !== STATES.GREETING_OPENED) return;
      if (boxPointerInside) {
        pauseAndReset(waveVideo);
        try { boxVideo.currentTime = Math.max(0, boxVideo.duration - .04); } catch (error) {}
        setState(STATES.OPENED);
      } else {
        returnToIdle();
      }
    }

    const eyeResizeObserver = typeof ResizeObserver === 'function'
      ? new ResizeObserver(refreshEyeGeometry)
      : null;
    refreshEyeGeometry();
    eyeResizeObserver?.observe(art);
    eyeMasks.forEach(mask => eyeResizeObserver?.observe(mask));
    window.addEventListener('resize', refreshEyeGeometry, listenerOptions);
    art.querySelector('.idle-layer')?.addEventListener('load', refreshEyeGeometry, listenerOptions);

    boxHotspot.addEventListener('pointerenter', requestBoxOpening, listenerOptions);
    boxHotspot.addEventListener('pointerleave', beginBoxClosing, listenerOptions);
    boxHotspot.addEventListener('focus', requestBoxOpening, listenerOptions);
    boxHotspot.addEventListener('blur', beginBoxClosing, listenerOptions);
    boxHotspot.addEventListener('click', event => {
      event.stopPropagation();
      if (!coarsePointer) return;
      if (characterState === STATES.OPENING || characterState === STATES.OPENED) {
        boxPointerInside = false;
        beginBoxClosing();
      } else if (characterState === STATES.IDLE) {
        boxPointerInside = true;
        beginBoxOpening();
      }
    }, listenerOptions);
    greetHotspot.addEventListener('click', greetMikey, listenerOptions);
    boxVideo.addEventListener('ended', handleBoxEnded, listenerOptions);
    fullActionVideo.addEventListener('ended', handleFullActionEnded, listenerOptions);
    waveVideo.addEventListener('ended', handleWaveEnded, listenerOptions);
    videos.forEach(video => {
      video.addEventListener('error', () => {
        if (resourcesReady) returnToIdle();
      }, listenerOptions);
    });
    initializeCharacterResources();

    stage.querySelectorAll('[data-error-queue]').forEach(button => {
      button.addEventListener('click', () => $('#allErrBtn')?.click(), listenerOptions);
    });

    stage.querySelectorAll('[data-view-target]').forEach(button => {
      button.addEventListener('click', () => {
        const view = button.dataset.viewTarget;
        const tab = document.querySelector(`.workspace-tab[data-view="${view}"]`);
        if (tab) tab.click();
        if (button.dataset.engine && $('#engineSel')) $('#engineSel').value = button.dataset.engine;
        const targetId = button.dataset.scrollTarget || (view === 'gaia' ? 'gaiaView' : 'doudianView');
        requestAnimationFrame(() => {
          const target = document.getElementById(targetId);
          if (target) {
            if (target.tagName === 'DETAILS') target.open = true;
            target.scrollIntoView({behavior: reducedMotion ? 'auto' : 'smooth', block: 'start'});
          }
        });
      }, listenerOptions);
    });
    function cleanup() {
      hoverTimer = clearTimer(hoverTimer);
      returnTimer = clearTimer(returnTimer);
      reducedGreetingTimer = clearTimer(reducedGreetingTimer);
      eyeReturnTimer = clearTimer(eyeReturnTimer);
      if (eyeFrame) window.cancelAnimationFrame(eyeFrame);
      eyeFrame = 0;
      eyeResizeObserver?.disconnect();
      boxPointerInside = false;
      hasPointerMoved = false;
      resourcesReady = false;
      transitionIntent = 'none';
      videos.forEach(pauseAndReset);
      controller.abort();
      art.getAnimations({subtree: true}).forEach(animation => animation.cancel());
      characterState = STATES.FALLBACK;
      art.dataset.characterState = STATES.FALLBACK;
      greetingNote?.classList.remove('is-visible');
      centerEyesImmediately();
    }
    window.addEventListener('pagehide', cleanup, {once: true});
  }

  function initGaiaWorkspace() {
    window.initGaiaWorkspace({$, toast});
  }

  /* ---------- 1. 工具函数 ---------- */
  function toast(msg, isErr) {
    const t = $('#toast');
    t.textContent = msg;
    t.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(t._h);
    t._h = setTimeout(() => { t.className = 'toast'; }, 3200);
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }
  // 报错项稳定 ID：按来源店铺 + 商品名 + 错误类型 + 型号 做哈希，刷新后不变，便于定位具体原因
  function errId(e) {
    const seed = [e.shop_id || '', e.name || '', e.error_type || '', e.model || ''].join('|');
    let h = 0;
    for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
    return 'E' + h.toString(16).toUpperCase().padStart(6, '0').slice(-6);
  }

  // 是否为「型号与预期不匹配」类异常：本地找不到型号目录、型号选择失败、
  // 发布前终检未通过、品牌/型号不完整，或 reason 里含“与预期不匹配/期望/终检”等字样。
  function isModelMismatch(e) {
    const et = e.error_type || '';
    if (et === '型号未匹配' || et === '型号选择失败' || et === '发布前型号终检未通过'
      || et === '品牌/型号不完整' || et === '型号异常') return true;
    const hay = (et + ' ' + (e.reason || '') + ' ' + (e.model || '')).toLowerCase();
    return /型号.{0,6}(与预期不匹配|不匹配|不符|终检|期望|选择失败)/.test(hay)
      || /(期望|预期).{0,6}型号/.test(hay);
  }
  // 型号不匹配时的红色高亮徽标文字（与“报错字样”一致）
  function mismatchBadgeText(e) {
    const et = e.error_type || '';
    if (et === '型号选择失败') return '型号选择失败';
    if (et === '发布前型号终检未通过') return '型号终检未通过';
    if (et === '品牌/型号不完整') return '品牌/型号不完整';
    if (et === '型号未匹配') return '型号未匹配';
    return /与预期不匹配/.test((et || '') + ' ' + (e.reason || ''))
      ? '型号与预期不匹配' : '型号不符';
  }
  // 生成型号标签 + （若不匹配）红色高亮徽标，两个报错弹窗复用
  function modelTagHtml(e) {
    let s = '';
    if (e.model) {
      const mm = isModelMismatch(e);
      s += '<span class="err-model-tag' + (mm ? ' mismatch' : '') + '">'
        + escapeHtml(e.model) + '</span>';
    }
    if (isModelMismatch(e)) {
      s += '<span class="badge-mismatch">' + mismatchBadgeText(e) + '</span>';
    }
    return s;
  }

  const STATE_TXT = {
    queued: '排队中', starting: '启动中', waiting_login: '等待扫码', running: '运行中',
    stopping: '停止中', done: '已完成', stopped: '已停止', error: '出错'
  };
  const STATE_CLS = {
    queued: 'st-queued', starting: 'st-running', waiting_login: 'st-waiting', running: 'st-running',
    stopping: 'st-stopping', done: 'st-done', stopped: 'st-stopped', error: 'st-error'
  };
  const ACTIVE = ['queued', 'starting', 'waiting_login', 'running', 'stopping'];
  const SLOT_ACTIVE = ['starting', 'waiting_login', 'running', 'stopping'];
  const isActive = s => ACTIVE.includes(s);

  // 状态 → 分组（出错也归「已停止」区，卡片本身红色徽章不变）
  const GROUP_OF = {
    queued: 'queued',
    starting: 'running', waiting_login: 'running', running: 'running', stopping: 'running',
    done: 'done', stopped: 'stopped', error: 'stopped'
  };

  /* ---------- 2. 危险确认弹窗 ---------- */
  let _pendingDanger = null;
  function showDanger(html, onConfirm) {
    $('#dangerMsg').innerHTML = html;
    $('#dangerModal').classList.add('show');
    _pendingDanger = onConfirm;
  }
  function closeDanger() {
    $('#dangerModal').classList.remove('show');
    _pendingDanger = null;
  }
  $('#dangerConfirm').onclick = () => { const f = _pendingDanger; closeDanger(); if (f) f(); };
  $('#dangerCancel').onclick = closeDanger;
  $('#dangerModal').addEventListener('click', e => { if (e.target === $('#dangerModal')) closeDanger(); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeDanger(); closeErr(); closeAllErr(); }
  });

  /* ---------- 2b. 报错明细弹窗 ---------- */
  const ERR_TITLES = {
    '型号未匹配': '型号未匹配',
    '主图白底图缺失': '主图白底图缺失',
    '型号选择失败': '型号选择失败',
    '发布前型号终检未通过': '发布前型号终检未通过',
    '品牌/型号不完整': '品牌/型号不完整',
    '发布商品失败': '发布商品失败',
    '处理异常': '处理异常',
    '型号异常': '型号异常'
  };
  // 排序：越具体的型号问题越靠前，越笼统的越靠后
  const ERR_ORDER = ['型号未匹配', '主图白底图缺失', '型号选择失败',
    '发布前型号终检未通过', '品牌/型号不完整', '发布商品失败', '处理异常', '型号异常'];
  const errorsCache = new Map();   // task_id -> 最近一次异常明细

  // 常见品牌兜底识别（后端旧异常数据没有 brand 字段时，从商品名提取）
  const BRAND_INFER_LIST = [
    ['苹果', 'IPHONE', 'APPLE'],
    ['华为', 'HUAWEI'],
    ['荣耀', 'HONOR'],
    ['小米', 'XIAOMI', 'REDMI', '红米'],
    ['OPPO'],
    ['vivo'],
    ['iQOO'],
    ['真我', 'REALME'],
    ['一加', 'ONEPLUS'],
    ['三星', 'SAMSUNG'],
    ['努比亚', 'NUBIA'],
    ['魅族', 'MEIZU'],
    ['索尼', 'SONY'],
    ['诺基亚', 'NOKIA'],
    ['摩托罗拉', 'MOTOROLA'],
    ['谷歌', 'GOOGLE'],
    ['华硕', 'ASUS'],
    ['黑鲨', 'BLACKSHARK'],
    ['中兴', 'ZTE'],
    ['联想', 'LENOVO'],
  ];
  function inferBrand(name) {
    if (!name) return '';
    const upper = name.toUpperCase();
    if (/IPHONE|苹果/.test(name)) return '苹果';
    for (const [cn, ...aliases] of BRAND_INFER_LIST) {
      if (name.includes(cn)) return cn;
      for (const a of aliases) {
        if (upper.includes(a)) return cn;
      }
    }
    return '';
  }

  function closeErr() { $('#errModal').classList.remove('show'); }
  $('#errClose').onclick = closeErr;
  $('#errCancel').onclick = closeErr;
  $('#errModal').addEventListener('click', e => { if (e.target === $('#errModal')) closeErr(); });

  function closeAllErr() { $('#allErrModal').classList.remove('show'); }
  $('#allErrClose').onclick = closeAllErr;
  $('#allErrCancel').onclick = closeAllErr;
  $('#allErrModal').addEventListener('click', e => { if (e.target === $('#allErrModal')) closeAllErr(); });

  // 全部报错汇总：错误类型组 / 品牌行 两级折叠小三角（事件委托，innerHTML 重建后仍生效）
  $('#allErrBody').addEventListener('click', e => {
    const gh = e.target.closest('.ae-group-head');
    if (gh) {
      gh.classList.toggle('collapsed');
      const body = gh.nextElementSibling;
      if (body && body.classList.contains('ae-group-body')) body.classList.toggle('collapsed');
      return;
    }
    const bh = e.target.closest('.ae-brand-head');
    if (bh) {
      bh.classList.toggle('collapsed');
      const body = bh.nextElementSibling;
      if (body && body.classList.contains('ae-brand-body')) body.classList.toggle('collapsed');
    }
  });

  // 底部联系区彩蛋：鼠标停留在视口最底部满 5s 才「砰」地弹出；离开 2.5s 收起
  function initContactEgg() {
    const strip = document.getElementById('contactStrip');
    if (!strip) return;
    const THRESH = 90, HOLD = 5000, FADE = 2500;  // 底部 90px / 悬停 5s / 收起 2.5s
    let hold = null, fade = null, over = false, last = 0;
    function reveal() { clearTimeout(hold); clearTimeout(fade); strip.classList.add('show'); }
    function beginHold() { if (strip.classList.contains('show')) return; clearTimeout(hold); hold = setTimeout(reveal, HOLD); }
    function endHold() { clearTimeout(hold); }
    function scheduleFade() { if (over) return; clearTimeout(fade); fade = setTimeout(() => strip.classList.remove('show'), FADE); }
    window.addEventListener('mousemove', e => {
      const now = Date.now();
      if (now - last < 180) return;   // 轻微节流
      last = now;
      if (e.clientY >= window.innerHeight - THRESH) { endHold(); beginHold(); }
      else { endHold(); scheduleFade(); }
    }, { passive: true });
    strip.addEventListener('mouseenter', () => { over = true; clearTimeout(fade); });
    strip.addEventListener('mouseleave', () => { over = false; scheduleFade(); });
  }

  // 报错明细：点击品牌行展开/折叠机型列表
  $('#errBody').addEventListener('click', e => {
    const row = e.target.closest('.err-brand-row');
    if (!row) return;
    const tid = row.dataset.toggle;
    const models = $('#' + tid);
    if (!models) return;
    const hidden = models.style.display === 'none';
    models.style.display = hidden ? 'flex' : 'none';
    const arrow = row.querySelector('.err-brand-arrow');
    if (arrow) arrow.textContent = hidden ? '▼' : '▶';
  });

  function renderErrorModal(errs) {
    const groups = {};
    errs.forEach(e => {
      const k = e.error_type || '其他';
      (groups[k] = groups[k] || []).push(e);
    });
    let html = '<div class="err-total">共 ' + errs.length + ' 条异常</div>';
    const keys = Object.keys(groups).sort(
      (a, b) => ERR_ORDER.indexOf(a) - ERR_ORDER.indexOf(b));
    keys.forEach((k, typeIdx) => {
      const list = groups[k];
      const title = ERR_TITLES[k] || k;

      // 按品牌二次分组（后端无 brand 字段时从商品名兜底识别）
      const brandGroups = {};
      list.forEach(e => {
        const b = e.brand || inferBrand(e.name) || '未知品牌';
        (brandGroups[b] = brandGroups[b] || []).push(e);
      });
      // 品牌按数量降序，相同数量按品牌名排序
      const brandKeys = Object.keys(brandGroups).sort((a, b) => {
        const d = brandGroups[b].length - brandGroups[a].length;
        return d !== 0 ? d : a.localeCompare(b, 'zh-CN');
      });

      html += '<div class="err-group">'
        + '<div class="err-group-title">' + escapeHtml(title) + ' (' + list.length + ')</div>'
        + '<div class="err-brand-rows">';
      brandKeys.forEach((b, brandIdx) => {
        const items = brandGroups[b];
        const tid = 'errb_' + typeIdx + '_' + brandIdx;
        html += '<div class="err-brand-row" data-toggle="' + tid + '">'
          + '<span class="err-brand-arrow">▶</span>'
          + '<span class="err-brand-name">' + escapeHtml(b) + '</span>'
          + '<span class="err-brand-count">' + items.length + '</span>'
          + '</div>'
          + '<div class="err-brand-models" id="' + tid + '">';
        items.forEach(e => {
          const eid = errId(e);
          const shopId = e.shop_id || '';
          const mm = isModelMismatch(e);
          const tip = escapeHtml((e.reason ? '原因：' + e.reason + '\n' : '')
            + (e.product_id ? '商品ID：' + e.product_id : ''));
          html += '<div class="err-model-row' + (mm ? ' mm' : '') + '" title="' + tip + '">'
            + '<span class="err-dot"></span>'
            + '<span class="eid" title="店铺 ID：' + escapeHtml(shopId) + '（定位 ID：' + eid + '）">' + escapeHtml(shopId) + '</span>'
            + '<span class="err-model-name">' + escapeHtml(e.name || '') + '</span>'
            + modelTagHtml(e)
            + (e.product_id ? '<span class="err-model-tag err-pid" title="商品 ID">ID ' + escapeHtml(String(e.product_id)) + '</span>' : '')
            + (e.reason ? '<span class="err-model-tag" style="border-color:rgba(251,113,133,.3);color:var(--danger);max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escapeHtml(String(e.reason)) + '</span>' : '')
            + '</div>';
        });
        html += '</div>';
      });
      html += '</div></div>';
    });
    $('#errBody').innerHTML = html;
    $('#errModal').classList.add('show');
  }

  function openErrors(tid) {
    // 每次都重新拉取最新报错明细，避免显示缓存中的旧数据。
    // 运行期间 SSE 仍会实时更新 errorsCache 用于计数闪烁提示，但点击查看时以服务端为准。
    fetch('/api/task/' + tid + '/errors')
      .then(r => r.json())
      .then(r => {
        const errs = r.errors || [];
        errorsCache.set(tid, errs);
        renderErrorModal(errs);
      })
      .catch(() => toast('加载报错明细失败', true));
  }

  // 查看全部报错：汇总已完成 / 已停止 / 出错 任务的所有异常，按错误类型 → 品牌 分组展示
  async function openAllErrors() {
    try {
      const r = await fetch('/api/errors/all').then(x => x.json());
      renderAllErrorsModal(r);
    } catch (e) { toast('加载全部报错失败', true); }
  }

  function renderAllErrorsModal(data) {
    const errs = data.errors || [];
    const tasksSum = data.tasks || [];
    let html = '';
    if (!errs.length) {
      html = '<div class="err-total" style="background:rgba(52,211,153,.08);'
        + 'border-color:rgba(52,211,153,.28);color:var(--success)">'
        + '🎉 当前没有已完成 / 已停止任务的报错记录</div>';
    } else {
      const byState = s => tasksSum.filter(t => t.state === s).length;
      html += '<div class="err-total">共 ' + errs.length + ' 条异常 · 来自 '
        + tasksSum.length + ' 个已结束任务（已完成 ' + byState('done')
        + ' / 已停止 ' + byState('stopped')
        + (byState('error') ? (' / 出错 ' + byState('error')) : '') + '）</div>';
      const groups = {};
      errs.forEach(e => { const k = e.error_type || '其他'; (groups[k] = groups[k] || []).push(e); });
      const keys = Object.keys(groups).sort((a, b) => ERR_ORDER.indexOf(a) - ERR_ORDER.indexOf(b));
      keys.forEach(k => {
        const list = groups[k];
        const title = ERR_TITLES[k] || k;
        const brandGroups = {};
        list.forEach(e => {
          const b = e.brand || inferBrand(e.name) || '未知品牌';
          (brandGroups[b] = brandGroups[b] || []).push(e);
        });
        const brandKeys = Object.keys(brandGroups).sort(
          (a, b) => brandGroups[b].length - brandGroups[a].length || a.localeCompare(b, 'zh-CN'));
        // 错误类型 → 品牌 两级均可折叠：类型组默认展开，品牌行默认折叠，防止数量过多堆叠
        html += '<div class="err-group">'
          + '<div class="ae-group-head" title="点击折叠/展开该错误类型">'
          + '<span class="ae-group-toggle">▶</span>'
          + '<span class="err-group-title">' + escapeHtml(title) + ' (' + list.length + ')</span>'
          + '</div>'
          + '<div class="ae-group-body">';
        brandKeys.forEach(b => {
          const arr = brandGroups[b];
          html += '<div class="ae-brand-head collapsed" title="点击折叠/展开该品牌">'
            + '<span class="ae-brand-arrow">▶</span>'
            + '<span class="err-brand-name">' + escapeHtml(b) + '</span>'
            + '<span class="err-brand-count">' + arr.length + '</span>'
            + '</div>'
            + '<div class="ae-brand-body collapsed">';
          arr.forEach(e => {
            const eid = errId(e);
            const shopId = e.shop_id || '';
            const mm = isModelMismatch(e);
            html += '<div class="err-row all' + (mm ? ' mm' : '') + '">'
              + '<span class="err-dot"></span>'
              + '<span class="eid" title="店铺 ID：' + escapeHtml(shopId) + '（定位 ID：' + eid + '）">' + escapeHtml(shopId) + '</span>'
              + '<span class="err-model-name">' + escapeHtml(e.name || '') + '</span>'
              + modelTagHtml(e)
              + (e.product_id ? '<span class="err-model-tag err-pid" title="抖店草稿商品 ID">ID ' + escapeHtml(String(e.product_id)) + '</span>' : '')
              + '<span class="err-model-tag shop-tag">' + escapeHtml(e.shop_name || e.shop_id || '') + '</span>'
              + (e.reason ? '<div class="err-reason-line">' + escapeHtml(String(e.reason)) + '</div>' : '')
              + '</div>';
          });
          html += '</div>';
        });
        html += '</div></div>';
      });
    }
    $('#allErrBody').innerHTML = html;
    $('#allErrModal').classList.add('show');
  }

  /* ---------- 3. 渲染 ---------- */

  // 顶栏并行槽位占用与空状态
  function refreshSlots() {
    const n = [...tasks.values()].filter(t => SLOT_ACTIVE.includes(t.state)).length;
    const queued = [...tasks.values()].filter(t => t.state === 'queued').length;
    $('#slotBadge').textContent = '并行 ' + n + ' / ' + MAXP + (queued ? ' · 排队 ' + queued : '');
    $('#runBtn').disabled = false;
    $('#empty').style.display = tasks.size ? 'none' : 'block';
    updateCoderSpeed();
    updateAllErrBadge();
  }

  // 右下角 coder 动图调速：0 任务 → 静止待机；1 任务 → 0.5x；
  // 随进行中任务数加速，≥4 个封顶 1.5x（精灵图 8 帧 / 960ms 为 1x 基准）
  const _coder = document.querySelector('.coder-gif');
  function updateCoderSpeed() {
    if (!_coder) return;
    const n = [...tasks.values()].filter(t => SLOT_ACTIVE.includes(t.state)).length;
    const wrap = _coder.parentElement;
    if (n <= 0) {
      wrap.classList.add('idle');
      _coder.style.animationPlayState = 'paused';
      return;
    }
    wrap.classList.remove('idle');
    const rate = Math.min(1.5, 0.5 + (n - 1) * (1.0 / 3)); // n=1→0.5x, n=4→1.5x
    _coder.style.animationDuration = (960 / rate).toFixed(0) + 'ms';
    _coder.style.animationPlayState = 'running';
  }

  // 顶栏「查看所有报错」按钮上的实时计数徽标：统计已结束（done/stopped/error）任务的 error_count 之和
  function updateAllErrBadge() {
    const btn = $('#allErrBtn');
    if (!btn) return;
    let total = 0;
    tasks.forEach(t => {
      if (t.state === 'done' || t.state === 'stopped' || t.state === 'error') {
        total += (t.error_count || 0);
      }
    });
    const badge = btn.querySelector('.ae-count');
    if (badge) badge.textContent = total;
    btn.disabled = false;
  }

  // 任务卡片（按 task_id 复用，避免重复创建）
  function cardEl(t) {
    let el = document.getElementById('c-' + t.id);
    if (el) return el;
    el = document.createElement('div');
    el.className = 'card';
    el.id = 'c-' + t.id;
    el.innerHTML = `
      <div class="card-head">
        <span class="shop"></span>
        <span class="meta"></span>
        <div class="spacer"></div>
        <span class="st"></span>
      </div>
      <div class="card-body">
        <div class="prow">
          <div class="track"><div class="fill"></div></div>
          <span class="pct">0%</span>
        </div>
        <div class="stats">
          <span><i>成功</i><b class="s-ok">0</b></span>
          <span><i>失败</i><b class="s-fail">0</b></span>
          <span><i>已处理</i><b class="s-done">0</b></span>
          <span><i>总计</i><b class="s-total">0</b></span>
        </div>
        <div class="cur"></div>
        <div class="term"></div>
      </div>
      <div class="card-foot">
        <button class="btn btn-sm btn-danger btn-stop">⏹ 温和停止</button>
        <button class="btn btn-sm btn-warn btn-clear">🗑 清除该店日志</button>
        <button class="btn btn-sm btn-warn btn-clear-task" style="display:none">🧹 清除本次日志</button>
        <button class="btn btn-sm btn-info btn-err" style="display:none">⚠ 查看报错</button>
        <button class="btn btn-sm btn-del">移除卡片</button>
        <span class="foot-info"></span>
      </div>`;

    el.querySelector('.btn-stop').onclick = async () => {
      const res = await fetch('/api/stop', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: t.id })
      }).then(r => r.json());
      toast(res.message, !res.success);
    };

    el.querySelector('.btn-clear').onclick = () => {
      const cur = tasks.get(t.id) || t;
      const shopName = cur.shop_name || cur.shop_id || '当前店铺';
      if (!cur.shop_id) {
        toast('该任务未指定店铺 ID，无法定向清除', true);
        return;
      }
      showDanger(
        `即将清除店铺 <strong>${escapeHtml(shopName)}</strong>（ID: ${escapeHtml(cur.shop_id)}）的本地运行日志。<br><br>` +
        `将删除：<br>` +
        `· 已处理商品记录 products_${escapeHtml(cur.shop_id)}.json<br>` +
        `· 运行调试日志 panel_data/logs/*.log<br>` +
        `· 该卡片内的日志显示<br><br>` +
        `<strong>后果：该店铺全部商品会被重新视为「未处理」，下次批量运行将从头重跑。此操作不可撤销。</strong>`,
        async () => {
          try {
            const res = await fetch('/api/clear-logs', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ shop_id: cur.shop_id })
            }).then(r => r.json());
            toast(res.message || '已清除', !res.success);
            if (res.success) {
              const term = el.querySelector('.term');
              if (term) term.innerHTML = '';
            }
          } catch (e) { toast('清除失败：' + e, true); }
        }
      );
    };

    el.querySelector('.btn-clear-task').onclick = async () => {
      const cur = tasks.get(t.id) || t;
      showDanger(
        `仅清除本次运行（ID: ${escapeHtml(cur.id)}）的<a href="javascript:void(0)">终端日志</a>和调试日志文件。<br><br>` +
        `将清除：<br>` +
        `· 该卡片内的终端日志显示<br>` +
        `· 本次运行的 debug_log 文件<br>` +
        `· 本次运行的报错明细<br><br>` +
        `<strong style="color:#22c55e">不会清除：</strong>已处理商品记录 products_${escapeHtml(cur.shop_id || '')}.json（已成功的商品不会重跑）`,
        async () => {
          try {
            const res = await fetch('/api/clear-task-logs', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ task_id: cur.id })
            }).then(r => r.json());
            if (res.success) {
              const term = el.querySelector('.term');
              if (term) term.innerHTML = '';
              const errBtn = el.querySelector('.btn-err');
              if (errBtn) { errBtn.style.display = 'none'; errBtn.textContent = '⚠ 查看报错'; }
              toast(res.message || '已清除本次日志');
            } else {
              toast(res.message || '清除失败', true);
            }
          } catch (e) { toast('清除失败：' + e, true); }
        }
      );
    };

    el.querySelector('.btn-del').onclick = async () => {
      const res = await fetch('/api/task/' + t.id, { method: 'DELETE' }).then(r => r.json());
      if (!res.success) toast(res.message, true);
    };

    el.querySelector('.btn-err').onclick = () => openErrors(t.id);
    return el;
  }

  // 按当前状态把卡片挂到对应分组容器（仅跨组时搬动，避免频繁跳动）
  function placeCard(el, state) {
    const g = GROUP_OF[state] || 'stopped';
    const box = document.getElementById('g-' + g);
    if (box && el.parentElement !== box) box.prepend(el);
  }

  // 刷新各分组计数，并隐藏没有任务的分组
  function refreshGroups() {
    const counts = { queued: 0, running: 0, done: 0, stopped: 0 };
    tasks.forEach(t => { counts[GROUP_OF[t.state] || 'stopped']++; });
    ['queued', 'running', 'done', 'stopped'].forEach(g => {
      const sec = document.querySelector('.task-group[data-group="' + g + '"]');
      const cnt = document.getElementById('cnt-' + g);
      if (cnt) cnt.textContent = counts[g];
      if (sec) sec.style.display = counts[g] ? 'block' : 'none';
    });
    const queueBox = document.getElementById('g-queued');
    if (queueBox) {
      [...tasks.values()].filter(t => t.state === 'queued')
        .sort((a, b) => (a.queue_position || 9999) - (b.queue_position || 9999))
        .forEach(t => {
          const card = document.getElementById('c-' + t.id);
          if (card) queueBox.appendChild(card);
        });
    }
  }

  // 分组折叠（展开/收起）：防止单组卡片过多导致页面过长；记忆用户偏好
  function loadTgCollapsed() {
    try { return JSON.parse(localStorage.getItem('tgCollapsed') || '{}') || {}; }
    catch (e) { return {}; }
  }
  const tgCollapsed = loadTgCollapsed();
  function applyTgCollapse(sec) {
    const g = sec.dataset.group;
    const collapsed = !!tgCollapsed[g];
    sec.classList.toggle('collapsed', collapsed);
    const btn = sec.querySelector('.tg-toggle');
    if (btn) {
      btn.textContent = collapsed ? '▸' : '▾';
      btn.setAttribute('aria-expanded', String(!collapsed));
    }
  }
  function initGroupToggles() {
    document.querySelectorAll('.task-group').forEach(sec => {
      const head = sec.querySelector('.tg-head');
      if (!head) return;
      if (!head.querySelector('.tg-toggle')) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'tg-toggle';
        btn.title = '展开 / 收起该分组';
        head.appendChild(btn);
      }
      head.addEventListener('click', () => {
        const g = sec.dataset.group;
        tgCollapsed[g] = !tgCollapsed[g];
        try { localStorage.setItem('tgCollapsed', JSON.stringify(tgCollapsed)); } catch (e) {}
        applyTgCollapse(sec);
      });
      applyTgCollapse(sec);
    });
  }

  // 用 brief 更新整张卡片
  function renderTask(t) {
    tasks.set(t.id, Object.assign({}, tasks.get(t.id) || {}, t));
    const el = cardEl(t);
    placeCard(el, t.state);
    const p = t.progress || { total: 0, processed: 0, success: 0, failed: 0, current: '' };

    el.querySelector('.shop').textContent =
      t.shop_name || (t.shop_id ? '店铺 ' + t.shop_id : '当前登录店铺');
    el.querySelector('.meta').textContent =
      (t.shop_id ? 'ID:' + t.shop_id + '  ' : '') +
      (t.engine === 'api_media' ? 'API 素材' : '浏览器') + ' · ' +
      (t.mode === 'test' ? '测试 ×' + t.test_n : '批量') + '  ' +
      (t.state === 'queued' ? '排队第 ' + (t.queue_position || '-') + ' 位' : '槽位' + t.slot);

    const st = el.querySelector('.st');
    st.className = 'st ' + (STATE_CLS[t.state] || 'st-stopped');
    st.innerHTML = (isActive(t.state) ? '<span class="pulse"></span>' : '') +
      (STATE_TXT[t.state] || t.state);

    const pct = p.total ? Math.round(p.processed / p.total * 100) : 0;
    const fill = el.querySelector('.fill');
    fill.style.width = pct + '%';
    fill.className = 'fill' + (t.state === 'done' ? ' done' : '');
    el.querySelector('.pct').textContent = pct + '%';
    el.querySelector('.s-ok').textContent = p.success || 0;
    el.querySelector('.s-fail').textContent = p.failed || 0;
    el.querySelector('.s-done').textContent = p.processed || 0;
    el.querySelector('.s-total').textContent = p.total || 0;
    el.querySelector('.cur').innerHTML = t.state === 'queued'
      ? '等待中：<b>有执行槽位后自动启动，无需再次点击</b>'
      : (p.current ? '正在处理：<b>' + escapeHtml(p.current) + '</b>' : '');
    const stopButton = el.querySelector('.btn-stop');
    stopButton.disabled = !isActive(t.state) || t.stop_requested;
    stopButton.textContent = t.state === 'queued' ? '取消排队' : '⏹ 温和停止';
    el.querySelector('.btn-clear').disabled = isActive(t.state);
    el.querySelector('.btn-del').disabled = isActive(t.state);
    el.querySelector('.foot-info').textContent =
      (t.message || '') + (t.debug_log ? '   日志: ' + t.debug_log : '');

    // 「查看报错」按钮：运行完成后始终显示（方便回看异常明细）
    const errBtn = el.querySelector('.btn-err');
    const ec = t.error_count || 0;
    // 已完成 + 有异常：显示计数；已完成 + 无异常：显示按钮但无计数；运行中：隐藏
    const isDone = (t.state === 'stopped' || t.state === 'done' || t.state === 'error');
    if (isDone && ec > 0) {
      errBtn.style.display = '';
      errBtn.textContent = '⚠ 查看报错 (' + ec + ')';
      errBtn.classList.add('pulse-err');
    } else if (isDone) {
      errBtn.style.display = '';
      errBtn.textContent = '📋 查看结果';
      errBtn.classList.remove('pulse-err');
    } else {
      errBtn.style.display = 'none';
    }

    // 「清除本次日志」按钮：任务已启动过（有日志行或已结束）才显示
    const clearTaskBtn = el.querySelector('.btn-clear-task');
    const hasLogs = (t.log_count || 0) > 0 || isDone;
    clearTaskBtn.style.display = hasLogs && !isActive(t.state) ? '' : 'none';

    refreshSlots();
    refreshGroups();
  }

  // 型号异常日志关键词（与 panel_runner.py 的 _MODEL_MM_RE 保持一致）
  const MODEL_MM_RE = /型号选择失败|型号=FAIL|品牌\/型号不完整|型号未匹配|发布前型号终检未通过|与预期不匹配|期望型号|型号.*不匹配/;
  // 对日志消息中的型号异常关键词加内联红字高亮
  function highlightModelMismatchHtml(msg) {
    let s = escapeHtml(msg);
    const patterns = ['型号选择失败', '型号=FAIL', '品牌/型号不完整', '型号未匹配',
      '发布前型号终检未通过', '与预期不匹配', '期望型号'];
    patterns.forEach(p => {
      s = s.split(p).join('<span class="mm-text">' + p + '</span>');
    });
    return s;
  }

  // 追加一条日志（自动滚动到底部）
  function appendLog(tid, log) {
    const el = document.getElementById('c-' + tid);
    if (!el) return;
    const term = el.querySelector('.term');
    const near = term.scrollHeight - term.scrollTop - term.clientHeight < 60;
    const msg = log.msg || '';
    const isMm = MODEL_MM_RE.test(msg);
    const d = document.createElement('div');
    d.className = 'ln ' + (log.level || 'info') + (isMm ? ' mm' : '');
    d.innerHTML = '<span class="tm">' + escapeHtml(log.time || '') + '</span>' +
      '<span class="tg">' + escapeHtml(log.tag || '') + '</span>' +
      '<span class="mg">' + (isMm ? highlightModelMismatchHtml(msg) : escapeHtml(msg)) + '</span>';
    term.appendChild(d);
    while (term.childElementCount > 1200) term.removeChild(term.firstChild);
    if (near) term.scrollTop = term.scrollHeight;
  }

  async function loadLogs(tid) {
    try {
      const r = await fetch('/api/task/' + tid + '/logs').then(r => r.json());
      if (!r || !r.logs) return;
      const el = document.getElementById('c-' + tid);
      if (!el) return;
      const term = el.querySelector('.term');
      term.innerHTML = '';
      r.logs.forEach(l => appendLog(tid, l));
    } catch (e) { /* ignore */ }
  }

  /* ---------- 4. 控制区交互 ---------- */
  async function loadShops() {
    const r = await fetch('/api/shops').then(r => r.json());
    MAXP = r.max_parallel || 2;
    const sel = $('#shopSel');
    r.shops.forEach(s => {
      const o = document.createElement('option');
      o.value = s.id; o.textContent = s.name + '  (' + s.id + ')';
      sel.appendChild(o);
      const gaiaSel = $('#gaiaShop');
      if (gaiaSel) {
        const go = document.createElement('option');
        go.value = s.id; go.textContent = s.name + '  (' + s.id + ')';
        gaiaSel.appendChild(go);
      }
    });
    refreshSlots();
  }

  // 模式分段控件（测试 / 批量）
  function initModeSegment() {
    const seg = $('#modeSeg');
    seg.querySelectorAll('.seg').forEach(b => {
      b.onclick = () => {
        seg.querySelectorAll('.seg').forEach(x => x.classList.remove('active'));
        b.classList.add('active');
        $('#nWrap').style.display = b.dataset.mode === 'test' ? 'flex' : 'none';
      };
    });
  }

  $('#runBtn').onclick = async () => {
    const active = $('#modeSeg').querySelector('.seg.active');
    const mode = active ? active.dataset.mode : 'test';
    const body = {
      shop_id: $('#shopSel').value,
      engine: $('#engineSel').value,
      mode: mode,
      test_n: parseInt($('#testN').value) || 1,
      release_stage: $('#releaseStage').value,
      release_stage_explicit: Boolean($('#releaseStage').value),
      product_id: ($('#productId').value || '').trim(),
      save_only: $('#saveOnly').checked
    };
    if (body.release_stage === 'full' && !confirm('全量仅应在离线回归和 10/20 件验证均通过后启动。确认继续？')) return;
    $('#runBtn').disabled = true;
    const res = await fetch('/api/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(r => r.json()).catch(() => ({ success: false, message: '请求失败' }));
    toast(res.message, !res.success);
    if (res.success && res.task) renderTask(res.task);
    refreshSlots();
  };

  $('#smartRunBtn').onclick = async () => {
    const shopId = $('#shopSel').value;
    if (!shopId) { toast('请先选择店铺', true); return; }
    const safe = $('#saveOnly').checked;
    const engine = $('#engineSel').value;
    const action = safe ? '只保存草稿，不会发布' : '将尝试正式发布';
    const route = engine === 'api_media'
      ? '将通过 API 读取最新草稿、上传或复用素材，并在回读核验后处理 1 件'
      : '将扫描所选店铺草稿箱并通过浏览器处理 1 件';
    if (!confirm(`${route}；${action}。无法证明图片完整或不符合规则的商品会被标注，不会冒险提交。是否继续？`)) return;
    const btn = $('#smartRunBtn');
    btn.disabled = true;
    const res = await fetch('/api/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        shop_id: shopId,
        engine: engine,
        mode: 'test',
        test_n: 1,
        product_id: ($('#productId').value || '').trim(),
        save_only: $('#saveOnly').checked
      })
    }).then(r => r.json()).catch(() => ({ success: false, message: '请求失败' }));
    btn.disabled = false;
    toast(res.message, !res.success);
    if (res.success && res.task) renderTask(res.task);
    refreshSlots();
  };

  /* ---------- 5. SSE 实时通信 ---------- */
  function connect() {
    const es = new EventSource('/api/stream');
    es.onopen = () => { $('#dot').className = 'dot on'; $('#connTxt').textContent = '已连接'; };
    es.onerror = () => { $('#dot').className = 'dot off'; $('#connTxt').textContent = '重连中'; };
    es.onmessage = ev => {
      let d;
      try { d = JSON.parse(ev.data); } catch (e) { return; }
      if (d.type === 'snapshot') {
        d.tasks.forEach(t => { renderTask(t); loadLogs(t.id); });
        refreshSlots();
      } else if (d.type === 'log') {
        appendLog(d.task_id, d.log);
      } else if (d.type === 'state' || d.type === 'progress') {
        renderTask(d.task);
      } else if (d.type === 'removed') {
        tasks.delete(d.task_id);
        const el = document.getElementById('c-' + d.task_id);
        if (el) el.remove();
        refreshSlots();
        refreshGroups();
      } else if (d.type === 'logs_cleared') {
        toast('已清除店铺 ' + d.shop_id + ' 的 ' + d.count + ' 个日志文件');
      } else if (d.type === 'task_logs_cleared') {
        const el = document.getElementById('c-' + d.task_id);
        if (el) {
          const term = el.querySelector('.term');
          if (term) term.innerHTML = '';
          const errBtn = el.querySelector('.btn-err');
          if (errBtn) { errBtn.style.display = 'none'; errBtn.textContent = '⚠ 查看报错'; }
          const clearBtn = el.querySelector('.btn-clear-task');
          if (clearBtn) clearBtn.style.display = 'none';
        }
        toast('已清除本次运行日志');
      } else if (d.type === 'errors') {
        if (d.task) renderTask(d.task);
        if (d.errors) errorsCache.set(d.task_id, d.errors);
      }
    };
  }

  // SSE 负责低延迟更新；定时回查负责在浏览器休眠、网络切换或 SSE
  // 短暂断线后补齐最终状态，避免任务已经结束但卡片仍停留在“进行中”。
  let taskRefreshInFlight = false;
  async function refreshTaskSnapshot() {
    if (taskRefreshInFlight) return;
    taskRefreshInFlight = true;
    try {
      const data = await fetch('/api/tasks', { cache: 'no-store' }).then(r => r.json());
      (data.tasks || []).forEach(t => {
        const previous = tasks.get(t.id);
        const logCountChanged = !previous || previous.log_count !== t.log_count;
        renderTask(t);
        if (logCountChanged) loadLogs(t.id);
      });
      refreshSlots();
    } catch (e) { /* SSE 仍会继续自动重连 */ }
    finally { taskRefreshInFlight = false; }
  }

  /* ---------- 6. 初始化 ---------- */
  loadShops().then(() => { connect(); refreshTaskSnapshot(); });
  initWorkspaceNavigation();
  initGaiaWorkspace();
  initModeSegment();
  initGroupToggles();
  initContactEgg();                   // 底部联系区彩蛋（底部悬停 5s 弹出）
  setInterval(refreshTaskSnapshot, 3000); // SSE 断线时兜底同步任务完成状态

  // 顶栏「查看所有报错」按钮
  const allErrBtn = $('#allErrBtn');
  if (allErrBtn) allErrBtn.onclick = openAllErrors;

})();
