/* MYRAA sounds: opt-in, CRUD confirmation only; alerts vibrate on supported devices.
   General navigation, search typing, view/scan and every button click stay silent. */
(() => {
  'use strict';
  const KEY = 'myraaSellerSoundEnabled';
  let enabled = false;
  try { enabled = localStorage.getItem(KEY) === '1'; } catch (_) {}
  let context = null;
  let lastCrud = 0;
  const button = document.getElementById('sound-toggle');
  function draw() {
    if (!button) return;
    button.setAttribute('aria-pressed', String(enabled));
    button.innerHTML = enabled
      ? '<i class="fa-solid fa-volume-high" aria-hidden="true"></i> Sound on'
      : '<i class="fa-solid fa-volume-xmark" aria-hidden="true"></i> Sound off';
    button.title = enabled ? 'Turn off save/delete/alert sounds' : 'Turn on save/delete/alert sounds';
  }
  function tone(frequency = 880, duration = .10) {
    if (!enabled) return;
    try {
      const Constructor = window.AudioContext || window.webkitAudioContext;
      if (!Constructor) return;
      context = context || new Constructor();
      if (context.state === 'suspended') void context.resume();
      const osc = context.createOscillator();
      const gain = context.createGain();
      const at = context.currentTime;
      osc.type = 'sine'; osc.frequency.value = frequency;
      gain.gain.setValueAtTime(.0001, at);
      gain.gain.exponentialRampToValueAtTime(.042, at + .015);
      gain.gain.exponentialRampToValueAtTime(.0001, at + duration);
      osc.connect(gain).connect(context.destination);
      osc.start(at); osc.stop(at + duration + .015);
    } catch (_) { /* Browser audio restrictions must never block a save. */ }
  }
  function success() {
    const now = Date.now();
    if (now - lastCrud < 280) return; // one sound per completed operation
    lastCrud = now;
    tone(890, .11);
  }
  function alert() {
    if (document.visibilityState === 'hidden') return;
    tone(730, .18);
    // Vibration may require prior user activation and is unsupported on some
    // browsers (notably iOS Safari). It is a best-effort optional signal.
    try { if (typeof navigator.vibrate === 'function') navigator.vibrate([100, 55, 160]); } catch (_) {}
  }
  button?.addEventListener('click', () => {
    enabled = !enabled;
    try { localStorage.setItem(KEY, enabled ? '1' : '0'); } catch (_) {}
    draw();
  });
  window.myraaSound = {
    success,
    error() { /* no generic sounds for read-only or navigation failures */ },
    alert,
    click() { /* compatibility: intentionally silent */ },
    isEnabled() { return enabled; },
  };
  draw();
})();
