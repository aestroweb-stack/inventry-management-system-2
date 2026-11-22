/* MYRAA premium account menu. Uses existing authenticated profile/logout flows. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const trigger = byId('workspace-user-profile');
  const menu = byId('workspace-account-menu');
  const wrapper = byId('workspace-account');
  if (!trigger || !menu || !wrapper) return;
  const profile = byId('workspace-account-profile');
  const signout = byId('workspace-account-signout');
  const items = [profile, signout].filter(Boolean);
  let isSigningOut = false;

  function setOpen(open, restoreFocus = false) {
    menu.hidden = !open;
    trigger.setAttribute('aria-expanded', String(open));
    wrapper.classList.toggle('is-open', open);
    if (restoreFocus) trigger.focus({preventScroll:true});
  }

  trigger.addEventListener('click', () => setOpen(menu.hidden));
  trigger.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();event.stopPropagation();setOpen(true);items[0]?.focus();
    }
  });
  document.addEventListener('pointerdown', event => {
    if (!menu.hidden && !wrapper.contains(event.target)) setOpen(false);
  });
  document.addEventListener('keydown', event => {
    if (menu.hidden) return;
    if (event.key === 'Escape') { event.preventDefault(); setOpen(false,true); }
    if (['ArrowDown','ArrowUp','Home','End'].includes(event.key) && items.includes(document.activeElement)) {
      event.preventDefault();
      const index = items.indexOf(document.activeElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length-1
        : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
    }
  });
  document.querySelectorAll('.rail-link[data-view]').forEach(button => {
    button.addEventListener('click', () => setOpen(false));
  });
  window.addEventListener('resize', () => { if (!menu.hidden && window.innerWidth < 320) setOpen(false); });

  profile?.addEventListener('click', () => {
    setOpen(false);
    // The existing sidebar action loads the authenticated seller's current profile.
    byId('workspace-profile-link')?.click();
  });
  signout?.addEventListener('click', () => {
    if (isSigningOut) return;
    isSigningOut = true;
    signout.disabled = true;
    setOpen(false);
    // The established Clerk handler clears the backend session and signs out of Clerk.
    const existingLogout = byId('logout-button') || byId('workspace-profile-logout');
    if (existingLogout) existingLogout.click();
    else {isSigningOut = false;signout.disabled = false;}
  });
  window.addEventListener('myraa:account', event => {
    const name = String(event.detail?.name || 'My account').trim();
    const email = String(event.detail?.email || '').trim();
    const title = byId('workspace-account-display-name');
    const subtitle = byId('workspace-account-email');
    if (title) title.textContent = name;
    if (subtitle) subtitle.textContent = email || 'Private seller workspace';
  });
})();
