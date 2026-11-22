/* MYRAA dashboard API auth gate — Clerk remains the ONLY identity provider.
   This intercepts same-origin private /api requests on the dashboard, not on
   the login page. No tokens, cookie values or secrets are copied or stored. */
(() => {
  'use strict';
  const nativeFetch = window.fetch.bind(window);
  let state = 'pending';
  let failure = null;
  let resolveInitial, rejectInitial;
  let renewSession = null;
  let refreshFlight = null;
  const initial = new Promise((resolve, reject) => {
    resolveInitial = resolve;
    rejectInitial = reject;
  });
  // Avoid an unhandled rejection when there are no pending API calls.
  initial.catch(() => {});

  function apiPath(input) {
    try {
      const rawUrl = typeof input === 'string' ? input : input.url;
      const target = new URL(rawUrl, window.location.href);
      if (target.origin !== window.location.origin || !target.pathname.startsWith('/api/')) return '';
      return target.pathname;
    } catch { return ''; }
  }
  function isPublic(path) {
    // Clerk handshake must be allowed to complete BEFORE private traffic.
    return path.startsWith('/api/clerk/') || path === '/api/health'
      || path === '/api/auth/status' || path === '/api/workspace-auth/status';
  }
  const gate = {
    ready() {
      if (state !== 'pending') return;
      state = 'ready';
      resolveInitial();
    },
    fail(error) {
      if (state === 'failed') return;
      failure = error instanceof Error ? error : new Error(String(error || 'MYRAA login required'));
      state = 'failed';
      rejectInitial(failure);
    },
    setRefresh(callback) { renewSession = callback; },
    async wait() {
      if (state === 'failed') throw failure;
      if (state === 'pending') await initial;
      // When a tab becomes active after idling, do not race a cookie refresh.
      if (refreshFlight) await refreshFlight;
      if (state === 'failed') throw failure;
    },
    async refresh() {
      if (state === 'failed') throw failure;
      if (!renewSession) throw new Error('Clerk session refresh not configured');
      if (!refreshFlight) {
        refreshFlight = Promise.resolve().then(() => renewSession()).finally(() => { refreshFlight = null; });
      }
      return refreshFlight;
    },
    get status() { return state; },
  };
  window.myraaAuthGate = gate;

  window.fetch = async (input, init) => {
    const path = apiPath(input);
    if (!path || isPublic(path)) return nativeFetch(input, init);
    await gate.wait();
    // Clone the Request so even uploads can be replayed exactly once when
    // the backend cookie expired. A 401 never processes the write server-side.
    const request = new Request(typeof input === 'string' ? new URL(input, window.location.href) : input, init);
    const first = await nativeFetch(request.clone());
    if (first.status !== 401) return first;
    try {
      await gate.refresh();
      const second = await nativeFetch(request.clone());
      if (second.status === 401) {
        gate.fail(new Error('MYRAA session expired after refresh'));
        window.location.replace('/app/sign-in.html');
      }
      return second;
    } catch (error) {
      gate.fail(error);
      window.location.replace('/app/sign-in.html');
      throw error;
    }
  };
})();
