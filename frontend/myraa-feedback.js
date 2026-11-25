/* MYRAA Feedback v1 — consistent notices and safe, async confirmations. */
(() => {
  'use strict';
  const TITLES = { success: 'Success', error: 'Something went wrong', warning: 'Attention needed', info: 'Just so you know' };
  const ICONS = {
    success: '<path d="M20 6 9 17l-5-5"/>',
    error: '<circle cx="12" cy="12" r="9"/><path d="M12 8v5m0 3h.01"/>',
    warning: '<path d="m12 3 10 17H2L12 3Z"/><path d="M12 9v4m0 3h.01"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/>'
  };
  const svg = (type) => `<svg viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[type] || ICONS.info}</svg>`;
  const kind = (type) => ({alert:'warning', danger:'error', true:'error'}[String(type)] || (TITLES[type] ? type : 'info'));
  let toastHost;
  function ensureToasts() {
    if (!toastHost?.isConnected) {
      toastHost = document.createElement('div');
      toastHost.className = 'myraa-notices';
      toastHost.setAttribute('aria-label', 'MYRAA notifications');
      document.body.append(toastHost);
    }
    return toastHost;
  }
  function notify(message, options = {}) {
    if (typeof options === 'string' || typeof options === 'boolean') options = { type: options };
    const type = kind(options.type || 'info');
    const body = String(message ?? '').trim();
    if (!body) return null;
    const host = ensureToasts();
    const existing = [...host.children].find(el => el.dataset.kind === type && el.dataset.message === body && !el.classList.contains('is-leaving'));
    if (existing) return existing; // Prevent duplicate error popups from concurrent callers.
    while (host.children.length >= 3) host.firstElementChild.remove();
    const card = document.createElement('section');
    card.className = `myraa-notice myraa-notice--${type}`;
    card.dataset.kind = type;
    card.dataset.message = body;
    card.setAttribute('role', type === 'error' || type === 'warning' ? 'alert' : 'status');
    const symbol = document.createElement('span');
    symbol.className = 'myraa-notice-icon';
    symbol.innerHTML = svg(type);
    const content = document.createElement('div');
    content.className = 'myraa-notice-text';
    const heading = document.createElement('strong');
    heading.textContent = options.title || TITLES[type];
    const detail = document.createElement('p');
    detail.textContent = body;
    content.append(heading, detail);
    const dismiss = document.createElement('button');
    dismiss.type = 'button';
    dismiss.className = 'myraa-notice-dismiss';
    dismiss.setAttribute('aria-label', 'Dismiss notification');
    dismiss.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>';
    card.append(symbol, content, dismiss);
    host.append(card);
    let timer, startedAt, remaining = Math.max(2600, Number(options.duration) || (type === 'success' ? 3700 : type === 'info' ? 4800 : 6500));
    let closed = false;
    function close() {
      if (closed) return;
      closed = true;
      clearTimeout(timer);
      card.classList.add('is-leaving');
      card.addEventListener('animationend', () => card.remove(), {once:true});
      setTimeout(() => card.remove(), 300);
    }
    function schedule() {
      if (closed) return;
      startedAt = Date.now();
      timer = setTimeout(close, remaining);
    }
    dismiss.addEventListener('click', close);
    card.addEventListener('mouseenter', () => { clearTimeout(timer); remaining = Math.max(250, remaining - (Date.now() - startedAt)); });
    card.addEventListener('mouseleave', schedule);
    card.addEventListener('focusin', () => clearTimeout(timer));
    card.addEventListener('focusout', (event) => { if (!card.contains(event.relatedTarget)) schedule(); });
    schedule();
    return card;
  }

  let confirmationActive = false;
  const confirmationQueue = [];
  function confirm(message, options = {}) {
    return new Promise((resolve) => {
      confirmationQueue.push({ message:String(message ?? ''), options, resolve });
      runConfirmationQueue();
    });
  }
  function runConfirmationQueue() {
    if (confirmationActive || !confirmationQueue.length) return;
    confirmationActive = true;
    const { message, options, resolve } = confirmationQueue.shift();
    const mode = options.variant === 'danger' ? 'danger' : options.variant === 'warning' ? 'warning' : 'info';
    const previousFocus = document.activeElement;
    const layer = document.createElement('div');
    layer.className = 'myraa-dialog-layer' + (options.layout === 'trash' ? ' myraa-dialog-layer--trash' : '');
    const dialog = document.createElement('section');
    dialog.className = `myraa-confirm myraa-confirm--${mode}`;
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const titleId = `myraa-confirm-title-${Date.now()}`;
    const bodyId = `myraa-confirm-body-${Date.now()}`;
    dialog.setAttribute('aria-labelledby', titleId);
    dialog.setAttribute('aria-describedby', bodyId);
    const top = document.createElement('div');
    top.className = 'myraa-confirm-top';
    const icon = document.createElement('span');
    icon.className = 'myraa-confirm-icon';
    icon.innerHTML = svg(mode === 'danger' ? 'warning' : mode);
    const texts = document.createElement('div');
    const eyebrow = document.createElement('span');
    eyebrow.className = 'myraa-confirm-eyebrow';
    eyebrow.textContent = mode === 'danger' ? 'PLEASE CONFIRM' : mode === 'warning' ? 'IMPORTANT ACTION' : 'MYRAA CONFIRMATION';
    const heading = document.createElement('h2');
    heading.id = titleId;
    heading.textContent = options.title || (mode === 'danger' ? 'Confirm this action' : 'Are you sure?');
    texts.append(eyebrow, heading);
    top.append(icon, texts);
    const body = document.createElement('p');
    body.id = bodyId;
    body.className = 'myraa-confirm-body';
    body.textContent = message;
    const buttons = document.createElement('div');
    buttons.className = 'myraa-confirm-actions';
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'myraa-confirm-cancel';
    cancel.textContent = options.cancelText || 'Cancel';
    const accept = document.createElement('button');
    accept.type = 'button';
    accept.className = 'myraa-confirm-accept';
    accept.textContent = options.confirmText || 'Continue';
    if (options.dismissOnly) buttons.append(accept);
    else buttons.append(cancel, accept);
    dialog.append(top, body, buttons);
    layer.append(dialog);
    document.body.append(layer);
    document.body.classList.add('myraa-dialog-open');
    let settled = false;
    function finish(result) {
      if (settled) return;
      settled = true;
      document.removeEventListener('keydown', onKeydown, true);
      layer.remove();
      document.body.classList.remove('myraa-dialog-open');
      if (previousFocus?.isConnected && typeof previousFocus.focus === 'function') previousFocus.focus({preventScroll:true});
      confirmationActive = false;
      resolve(result);
      runConfirmationQueue();
    }
    function onKeydown(event) {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); finish(false); return; }
      if (event.key !== 'Tab') return;
      if (options.dismissOnly) { event.preventDefault(); accept.focus(); return; }
      const controls = [cancel, accept];
      const index = controls.indexOf(document.activeElement);
      if (event.shiftKey && index <= 0) { event.preventDefault(); accept.focus(); }
      else if (!event.shiftKey && index >= controls.length - 1) { event.preventDefault(); cancel.focus(); }
    }
    document.addEventListener('keydown', onKeydown, true);
    layer.addEventListener('mousedown', (event) => { if (event.target === layer) finish(false); });
    cancel.addEventListener('click', () => finish(false));
    accept.addEventListener('click', () => finish(true));
    (options.dismissOnly ? accept : cancel).focus({ preventScroll:true }); // Never put focus on a destructive action first.
  }
  window.MYRAAUI = Object.freeze({
    notify,
    success: (message, options={}) => notify(message, {...options,type:'success'}),
    error: (message, options={}) => notify(message, {...options,type:'error'}),
    warning: (message, options={}) => notify(message, {...options,type:'warning'}),
    info: (message, options={}) => notify(message, {...options,type:'info'}),
    confirm,
    alert: (message, options={}) => confirm(message, {...options, dismissOnly:true, confirmText: options.confirmText || "OK"})
  });
})();
