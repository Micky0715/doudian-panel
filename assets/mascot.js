(() => {
  'use strict';

  const stage = document.getElementById('opsStage');
  const art = document.getElementById('mascotArt');
  if (!stage || !art) return;

  const forwardLayer = document.getElementById('characterForwardLayer');
  const idleLayer = document.getElementById('characterIdleLayer');
  const greetingNote = document.getElementById('mascotGreetingNote');
  const greetHotspot = document.getElementById('mascotGreetHotspot');
  const boxHotspot = document.getElementById('mascotBoxHotspot');
  const eyeMasks = Array.from(art.querySelectorAll('.character-eye-mask'));
  const boxVideo = document.getElementById('characterBoxLayer');
  const waveVideo = document.getElementById('characterFullActionLayer');
  const videos = [boxVideo, waveVideo].filter(Boolean);
  if (!forwardLayer || !idleLayer || !greetHotspot || !boxHotspot || eyeMasks.length !== 2 || videos.length !== 2) return;

  const STATES = Object.freeze({
    IDLE: 'idle',
    EYE_TRACKING: 'eyeTracking',
    BOX_OPENING: 'boxOpening',
    WAVING: 'waving',
    RETURNING: 'returning'
  });
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const coarsePointer = window.matchMedia('(pointer: coarse)').matches;
  const finePointer = window.matchMedia('(hover: hover) and (pointer: fine)').matches;
  const controller = new AbortController();
  const listenerOptions = {signal: controller.signal};
  const eyes = eyeMasks.map(mask => ({
    mask,
    iris: mask.querySelector('.character-iris'),
    sourceIrisWidth: Number(mask.dataset.irisWidth),
    sourceIrisHeight: Number(mask.dataset.irisHeight),
    noseDirection: Number(mask.dataset.noseDirection),
    sourceDownInset: Number(mask.dataset.downInset || 0),
    maxX: 0,
    maxY: 0,
    maxDownY: 0,
    targetX: 0,
    targetY: 0,
    currentX: 0,
    currentY: 0
  }));

  let characterState = STATES.IDLE;
  let resourcesReady = false;
  let videosReady = false;
  let gazeListenersEnabled = false;
  let hasPointerMoved = false;
  let pointerX = 0;
  let pointerY = 0;
  let eyeGroupCenterX = 0;
  let eyeGroupCenterY = 0;
  let eyeFrame = 0;
  let hoverTimer = 0;
  let returnTimer = 0;
  let noteTimer = 0;
  let boxPointerInside = false;
  let boxPlaybackFinished = false;
  let returnToIdleAfterCenter = false;

  function clearTimer(timer) {
    if (timer) window.clearTimeout(timer);
    return 0;
  }

  function setState(nextState) {
    characterState = nextState;
    art.dataset.characterState = nextState;
    boxHotspot.setAttribute('aria-pressed', String(nextState === STATES.BOX_OPENING));
  }

  function pauseAndReset(video) {
    if (!video) return;
    video.pause();
    try { video.currentTime = 0; } catch (error) {}
  }

  function centerEyesImmediately() {
    if (eyeFrame) window.cancelAnimationFrame(eyeFrame);
    eyeFrame = 0;
    eyes.forEach(eye => {
      eye.targetX = 0;
      eye.targetY = 0;
      eye.currentX = 0;
      eye.currentY = 0;
      eye.iris.style.setProperty('--gaze-x', '0px');
      eye.iris.style.setProperty('--gaze-y', '0px');
    });
  }

  function refreshEyeGeometry() {
    const maskBounds = eyes.map(eye => eye.mask.getBoundingClientRect());
    const artBounds = art.getBoundingClientRect();
    const scaleX = artBounds.width / 642;
    const scaleY = artBounds.height / 702;
    eyeGroupCenterX = maskBounds.reduce((sum, bounds) => sum + bounds.left + bounds.width / 2, 0) / maskBounds.length;
    eyeGroupCenterY = maskBounds.reduce((sum, bounds) => sum + bounds.top + bounds.height / 2, 0) / maskBounds.length;
    eyes.forEach((eye, index) => {
      const bounds = maskBounds[index];
      const irisBounds = eye.iris.getBoundingClientRect();
      const irisWidth = irisBounds.width * (eye.sourceIrisWidth / 32);
      const irisHeight = irisBounds.height * (eye.sourceIrisHeight / 32);
      const safetyInset = Math.max(2, Math.min(4, 3 * Math.min(scaleX, scaleY)));
      const safeRangeX = Math.max(0, (bounds.width - irisWidth) / 2 - safetyInset);
      const safeRangeY = Math.max(0, (bounds.height - irisHeight) / 2 - safetyInset);
      eye.maxX = safeRangeX * .85;
      eye.maxY = safeRangeY * .75;
      eye.maxDownY = Math.max(0, eye.maxY - eye.sourceDownInset * scaleY);
    });
  }

  function constrainToEye(eye, targetX, targetY) {
    if (!eye.maxX || !eye.maxY) return {x: 0, y: 0};
    let x = targetX;
    let y = targetY;
    const verticalRange = y > 0 ? eye.maxDownY : eye.maxY;
    if (!verticalRange) return {x: 0, y: 0};
    const ellipseValue = (x / eye.maxX) ** 2 + (y / verticalRange) ** 2;
    if (ellipseValue > 1) {
      const scale = 1 / Math.sqrt(ellipseValue);
      x *= scale;
      y *= scale;
    }
    return {x, y};
  }

  function updateEyeTargets() {
    if (!hasPointerMoved || characterState !== STATES.EYE_TRACKING) {
      eyes.forEach(eye => {
        eye.targetX = 0;
        eye.targetY = 0;
      });
      return;
    }
    const dx = pointerX - eyeGroupCenterX;
    const dy = pointerY - eyeGroupCenterY;
    const distance = Math.hypot(dx, dy);
    const strength = Math.min(
      distance / Math.max(1, Math.min(window.innerWidth, window.innerHeight) * .32),
      1
    );
    const directionX = dx / Math.max(distance, 1);
    const directionY = dy / Math.max(distance, 1);
    eyes.forEach(eye => {
      let targetX = directionX * eye.maxX * strength;
      const verticalRange = directionY > 0 ? eye.maxDownY : eye.maxY;
      let targetY = directionY * verticalRange * strength;
      if (targetX * eye.noseDirection > 0) targetX *= .93;
      const constrained = constrainToEye(eye, targetX, targetY);
      eye.targetX = constrained.x;
      eye.targetY = constrained.y;
    });
  }

  function drawEyes() {
    eyeFrame = 0;
    if (characterState !== STATES.EYE_TRACKING) return;
    const easing = .16;
    let moving = false;
    eyes.forEach(eye => {
      eye.currentX += (eye.targetX - eye.currentX) * easing;
      eye.currentY += (eye.targetY - eye.currentY) * easing;
      eye.iris.style.setProperty('--gaze-x', eye.currentX.toFixed(2) + 'px');
      eye.iris.style.setProperty('--gaze-y', eye.currentY.toFixed(2) + 'px');
      if (Math.abs(eye.targetX - eye.currentX) > .04 || Math.abs(eye.targetY - eye.currentY) > .04) moving = true;
    });
    if (moving) {
      eyeFrame = window.requestAnimationFrame(drawEyes);
    } else if (returnToIdleAfterCenter) {
      returnToIdleAfterCenter = false;
      centerEyesImmediately();
      setState(STATES.IDLE);
    }
  }

  function requestEyeFrame() {
    if (!eyeFrame) eyeFrame = window.requestAnimationFrame(drawEyes);
  }

  function handleGlobalPointerMove(event) {
    if (!resourcesReady || reducedMotion || coarsePointer || !finePointer) return;
    if (characterState === STATES.BOX_OPENING || characterState === STATES.WAVING || characterState === STATES.RETURNING) return;
    pointerX = event.clientX;
    pointerY = event.clientY;
    hasPointerMoved = true;
    returnToIdleAfterCenter = false;
    if (characterState === STATES.IDLE) setState(STATES.EYE_TRACKING);
    updateEyeTargets();
    requestEyeFrame();
  }

  function resetGaze() {
    if (characterState !== STATES.EYE_TRACKING) {
      hasPointerMoved = false;
      return;
    }
    hasPointerMoved = false;
    returnToIdleAfterCenter = true;
    updateEyeTargets();
    requestEyeFrame();
  }

  function handleVisibilityChange() {
    if (document.visibilityState === 'hidden') resetGaze();
  }

  function waitForImage(image) {
    if (image.complete && image.naturalWidth) {
      return typeof image.decode === 'function' ? image.decode().then(() => true, () => false) : Promise.resolve(true);
    }
    return new Promise(resolve => {
      image.addEventListener('load', () => resolve(true), {once: true, signal: controller.signal});
      image.addEventListener('error', () => resolve(false), {once: true, signal: controller.signal});
    });
  }

  function waitUntilPlayable(video) {
    if (video.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) return Promise.resolve(true);
    if (video.error) return Promise.resolve(false);
    return new Promise(resolve => {
      video.addEventListener('canplay', () => resolve(true), {once: true, signal: controller.signal});
      video.addEventListener('error', () => resolve(false), {once: true, signal: controller.signal});
      video.load();
    });
  }

  function enableGlobalGaze() {
    if (gazeListenersEnabled || reducedMotion || coarsePointer || !finePointer) return;
    gazeListenersEnabled = true;
    window.addEventListener('pointermove', handleGlobalPointerMove, {signal: controller.signal, passive: true});
    document.documentElement.addEventListener('pointerleave', resetGaze, listenerOptions);
    window.addEventListener('blur', resetGaze, listenerOptions);
    document.addEventListener('visibilitychange', handleVisibilityChange, listenerOptions);
  }

  async function initializeResources() {
    centerEyesImmediately();
    setState(STATES.IDLE);
    const imageResults = await Promise.all([
      waitForImage(forwardLayer),
      waitForImage(idleLayer),
      ...eyes.map(eye => waitForImage(eye.iris))
    ]);
    const videoResults = await Promise.all(videos.map(waitUntilPlayable));
    if (controller.signal.aborted) return;
    resourcesReady = imageResults.every(Boolean);
    videosReady = videoResults.every(Boolean);
    if (!resourcesReady) return;
    await new Promise(resolve => window.requestAnimationFrame(resolve));
    refreshEyeGeometry();
    centerEyesImmediately();
    setState(STATES.IDLE);
    enableGlobalGaze();
  }

  async function playExclusive(video, state, shouldStart) {
    if (!resourcesReady || !videosReady || reducedMotion || !shouldStart()) return false;
    hoverTimer = clearTimer(hoverTimer);
    hasPointerMoved = false;
    returnToIdleAfterCenter = false;
    if (eyeFrame) window.cancelAnimationFrame(eyeFrame);
    eyeFrame = 0;
    await new Promise(resolve => window.requestAnimationFrame(resolve));
    if (controller.signal.aborted || !shouldStart()) return false;
    videos.forEach(candidate => {
      if (candidate !== video) pauseAndReset(candidate);
    });
    pauseAndReset(video);
    centerEyesImmediately();
    setState(state);
    try {
      await video.play();
      return true;
    } catch (error) {
      returnToIdle();
      return false;
    }
  }

  function returnToIdle() {
    hoverTimer = clearTimer(hoverTimer);
    returnTimer = clearTimer(returnTimer);
    noteTimer = clearTimer(noteTimer);
    videos.forEach(pauseAndReset);
    greetingNote?.classList.remove('is-visible');
    boxPlaybackFinished = false;
    hasPointerMoved = false;
    returnToIdleAfterCenter = false;
    centerEyesImmediately();
    setState(STATES.RETURNING);
    returnTimer = window.setTimeout(() => {
      returnTimer = 0;
      if (characterState !== STATES.RETURNING) return;
      setState(STATES.IDLE);
    }, reducedMotion ? 80 : 140);
  }

  function requestBoxOpening() {
    boxPointerInside = true;
    hoverTimer = clearTimer(hoverTimer);
    if (!resourcesReady || !videosReady || reducedMotion) return;
    if (characterState !== STATES.IDLE && characterState !== STATES.EYE_TRACKING) return;
    hoverTimer = window.setTimeout(async () => {
      hoverTimer = 0;
      boxPlaybackFinished = false;
      await playExclusive(
        boxVideo,
        STATES.BOX_OPENING,
        () => boxPointerInside && (characterState === STATES.IDLE || characterState === STATES.EYE_TRACKING)
      );
    }, 120);
  }

  function closeBox() {
    boxPointerInside = false;
    hoverTimer = clearTimer(hoverTimer);
    if (characterState === STATES.BOX_OPENING) returnToIdle();
  }

  function showGreetingNote() {
    noteTimer = clearTimer(noteTimer);
    greetingNote?.classList.add('is-visible');
    noteTimer = window.setTimeout(() => {
      noteTimer = 0;
      greetingNote?.classList.remove('is-visible');
    }, 2500);
  }

  async function startGreeting() {
    if (!resourcesReady) return;
    if (reducedMotion) {
      showGreetingNote();
      return;
    }
    if (characterState === STATES.BOX_OPENING) {
      if (!boxPlaybackFinished) return;
      pauseAndReset(boxVideo);
      centerEyesImmediately();
      setState(STATES.RETURNING);
      await new Promise(resolve => {
        returnTimer = window.setTimeout(() => {
          returnTimer = 0;
          resolve();
        }, 140);
      });
    }
    if (characterState !== STATES.IDLE && characterState !== STATES.EYE_TRACKING && characterState !== STATES.RETURNING) return;
    const started = await playExclusive(
      waveVideo,
      STATES.WAVING,
      () => characterState === STATES.IDLE || characterState === STATES.EYE_TRACKING || characterState === STATES.RETURNING
    );
    if (started) showGreetingNote();
  }

  function handleBoxEnded() {
    if (characterState !== STATES.BOX_OPENING) return;
    boxVideo.pause();
    boxPlaybackFinished = true;
    try { boxVideo.currentTime = Math.max(0, boxVideo.duration - .04); } catch (error) {}
    if (!boxPointerInside) returnToIdle();
  }

  function handleWaveEnded() {
    if (characterState === STATES.WAVING) returnToIdle();
  }

  const resizeObserver = typeof ResizeObserver === 'function' ? new ResizeObserver(refreshEyeGeometry) : null;
  resizeObserver?.observe(art);
  eyes.forEach(eye => resizeObserver?.observe(eye.mask));
  window.addEventListener('resize', refreshEyeGeometry, listenerOptions);
  idleLayer.addEventListener('load', refreshEyeGeometry, listenerOptions);

  boxHotspot.addEventListener('pointerenter', requestBoxOpening, listenerOptions);
  boxHotspot.addEventListener('pointerleave', closeBox, listenerOptions);
  boxHotspot.addEventListener('focus', requestBoxOpening, listenerOptions);
  boxHotspot.addEventListener('blur', closeBox, listenerOptions);
  boxHotspot.addEventListener('click', event => {
    event.stopPropagation();
    if (!coarsePointer) return;
    if (characterState === STATES.BOX_OPENING) closeBox();
    else requestBoxOpening();
  }, listenerOptions);
  greetHotspot.addEventListener('click', event => {
    event.stopPropagation();
    startGreeting();
  }, listenerOptions);
  boxVideo.addEventListener('ended', handleBoxEnded, listenerOptions);
  waveVideo.addEventListener('ended', handleWaveEnded, listenerOptions);
  videos.forEach(video => {
    video.addEventListener('error', returnToIdle, listenerOptions);
  });

  stage.querySelectorAll('[data-error-queue]').forEach(button => {
    button.addEventListener('click', () => document.getElementById('allErrBtn')?.click(), listenerOptions);
  });
  stage.querySelectorAll('[data-view-target]').forEach(button => {
    button.addEventListener('click', () => {
      const view = button.dataset.viewTarget;
      document.querySelector(`.workspace-tab[data-view="${view}"]`)?.click();
      const engine = document.getElementById('engineSel');
      if (button.dataset.engine && engine) engine.value = button.dataset.engine;
      const targetId = button.dataset.scrollTarget || (view === 'gaia' ? 'gaiaView' : 'doudianView');
      window.requestAnimationFrame(() => {
        const target = document.getElementById(targetId);
        if (!target) return;
        if (target.tagName === 'DETAILS') target.open = true;
        target.scrollIntoView({behavior: reducedMotion ? 'auto' : 'smooth', block: 'start'});
      });
    }, listenerOptions);
  });

  function cleanup() {
    hoverTimer = clearTimer(hoverTimer);
    returnTimer = clearTimer(returnTimer);
    noteTimer = clearTimer(noteTimer);
    if (eyeFrame) window.cancelAnimationFrame(eyeFrame);
    eyeFrame = 0;
    resizeObserver?.disconnect();
    videos.forEach(pauseAndReset);
    controller.abort();
    art.getAnimations({subtree: true}).forEach(animation => animation.cancel());
    greetingNote?.classList.remove('is-visible');
    resourcesReady = false;
    videosReady = false;
    hasPointerMoved = false;
    centerEyesImmediately();
    setState(STATES.IDLE);
  }

  window.addEventListener('pagehide', cleanup, {once: true});
  initializeResources();
})();
