(() => {
  'use strict';
  const standalone = window.matchMedia('(display-mode: standalone)');
  const installed = () => standalone.matches || navigator.standalone === true;
  const triggers = [...document.querySelectorAll('[data-myraa-install]')];
  let pendingPrompt = null;
  let promptBusy = false;
  function sync() {
    document.documentElement.classList.toggle('myraa-standalone', installed());
    for (const button of triggers) button.hidden = installed();
  }
  function instructions() {
    let dialog = document.getElementById('myraa-install-dialog');
    if (!dialog) {
      dialog = document.createElement('dialog');
      dialog.id = 'myraa-install-dialog';
      dialog.setAttribute('aria-labelledby', 'myraa-install-title');
      dialog.innerHTML = `<div class="myraa-install-head"><img src="/app/icons/icon-192.png" alt="" width="56" height="56"><div><small>MYRAA INVENTORY</small><h2 id="myraa-install-title">Install MYRAA</h2></div></div><p>Open your inventory directly from your home screen.</p><div class="myraa-install-steps"><strong>Android / Chrome</strong><p>Open the browser menu (⋮), then choose Install app or Add to Home screen.</p><strong>iPhone / iPad</strong><p>In Safari, tap Share, then Add to Home Screen. Enable Open as Web App if shown.</p><strong>Desktop</strong><p>Use the install icon in the Chrome or Edge address bar, if available.</p></div><p class="myraa-install-note">Installation availability depends on your browser. Internet is required for inventory operations.</p><form method="dialog"><button type="submit">Close</button></form>`;
      document.body.append(dialog);
      dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
    }
    if (typeof dialog.showModal === 'function') { if (!dialog.open) dialog.showModal(); }
    else { window.alert('In Chrome, open the browser menu and choose Install app or Add to Home screen. In Safari, choose Share → Add to Home Screen.'); }
  }
  window.addEventListener('beforeinstallprompt', event => { event.preventDefault(); pendingPrompt = event; sync(); });
  window.addEventListener('appinstalled', () => { pendingPrompt = null; for (const button of triggers) button.hidden = true; });
  standalone.addEventListener?.('change', sync);
  for (const button of triggers) button.addEventListener('click', async () => {
    if (installed() || promptBusy) return;
    if (!pendingPrompt) { instructions(); return; }
    const prompt = pendingPrompt; pendingPrompt = null; promptBusy = true;
    try { await prompt.prompt(); const choice = await prompt.userChoice; if (choice?.outcome === 'accepted') for (const button of triggers) button.hidden = true; }
    catch { instructions(); }
    finally { promptBusy = false; }
  });
  sync();
  const notice = document.createElement('div');
  notice.id = 'myraa-offline-notice'; notice.setAttribute('role','status');
  notice.textContent = 'Offline · Reconnect before saving or changing stock.';
  document.body.append(notice);
  const updateConnection = () => { notice.hidden = navigator.onLine; };
  window.addEventListener('online', updateConnection); window.addEventListener('offline', updateConnection); updateConnection();
  if (window.isSecureContext && 'serviceWorker' in navigator) {
    navigator.serviceWorker.register('/app/sw.js', {scope:'/app/', updateViaCache:'none'}).catch(error => console.warn('MYRAA install support unavailable:', error));
  }
})();
