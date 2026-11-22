/* MYRAA Clerk auth: Google/Email-code/password identity + private seller workspace.
   No passwords, OAuth secrets, or long-lived session JWTs are stored in JS. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const loginPage = document.body.dataset.myraaClerk === 'login';
  const home = '/app/';
  const loginPageUrl = '/app/sign-in.html';
  // Both Clerk screens are mounted INSIDE the MYRAA sign-in page.
  // Explicit local URLs prevent Clerk's default Account Portal redirects.
  const localAuthUrl = (view) => `${loginPageUrl}?mode=${view === 'signup' ? 'signup' : 'signin'}`;
  let currentLoginView = null;
  const statusLine = (message) => {
    const el = $('clerk-auth-error');
    if (el) el.textContent = message || '';
  };
  const request = async (url, method='GET', body) => {
    const opt = {method, credentials:'same-origin', headers: {}};
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    const resp = await fetch(url,opt);
    const data = await resp.json().catch(()=>({}));
    if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
    return data;
  };
  const script = (src, attrs={}) => new Promise((resolve,reject) => {
    const item=document.createElement('script'); item.src=src; item.async=true; item.crossOrigin='anonymous';
    for (const [key,value] of Object.entries(attrs)) item.setAttribute(key,value);
    item.onload=resolve; item.onerror=()=>reject(new Error('Unable to load Clerk authentication. Check network and Clerk settings.'));
    document.head.appendChild(item);
  });
  const clerkConfig = async()=>{
    const resp=await request('/api/clerk/config');
    if (!resp.configured || !resp.publishableKey) throw new Error('Clerk is not configured in Railway Variables.');
    return resp.publishableKey;
  };
  let clerk, account=null, exchangeInFlight=null;
  const publicRoot = () => String(location.origin);
  const fetchStatus=()=>request('/api/workspace-auth/status');
  async function startClerk() {
    const publishableKey = await clerkConfig();
    const parts = publishableKey.split('_');
    if(parts.length < 3) throw new Error('Invalid Clerk publishable key');
    const frontendDomain = atob(parts.slice(2).join('_')).replace(/\$$/,'').replace(/\0+$/,'');
    if(!/^[a-z0-9.-]+$/i.test(frontendDomain)) throw new Error('Invalid Clerk frontend domain');
    // Official ClerkJS browser bundles; the publishable key is intentionally public.
    await script(`https://${frontendDomain}/npm/@clerk/ui@1/dist/ui.browser.js`);
    await script(`https://${frontendDomain}/npm/@clerk/clerk-js@6/dist/clerk.browser.js`, {
      'data-clerk-publishable-key':publishableKey,
    });
    clerk = window.Clerk;
    if(!clerk) throw new Error('Clerk SDK did not initialize');
    await clerk.load({ui:{ClerkUI:window.__internal_ClerkUICtor}});
    return clerk;
  }
  const hasSession = () => !!(clerk && clerk.session && clerk.user);
  async function removeBackendSession() {
    try { await request('/api/clerk/logout','POST'); } catch {} // Only clears browser cookies.
  }
  async function exchange() {
    if (!hasSession()) throw new Error('Clerk session not available. Sign in again.');
    // All simultaneous callers await the SAME exchange. A boolean `busy`
    // previously returned early, so API retries could run before cookies set.
    if (exchangeInFlight) return exchangeInFlight;
    exchangeInFlight = (async () => {
      // Clerk tokens are short lived. On 401, request one fresh token from
      // Clerk and retry exactly once. Never skip backend JWT verification.
      const submit = async (forceRefresh) => {
        const token = await clerk.session.getToken(forceRefresh ? {skipCache:true} : undefined);
        if (!token) throw new Error('Clerk session token not available. Sign in again.');
        const result = await fetch('/api/clerk/exchange', {method:'POST',credentials:'same-origin',
          headers:{'Authorization':`Bearer ${token}`,'Accept':'application/json'}});
        const payload = await result.json().catch(()=>({}));
        return {result,payload};
      };
      let {result,payload} = await submit(false);
      if (result.status === 401 && hasSession()) {
        ({result,payload} = await submit(true));
      }
      if(!result.ok) throw new Error(payload.detail || `HTTP ${result.status}`);
      account=payload.approved ? payload.user : null;
      if(payload.approved) {
        if(loginPage) location.replace(home);
        else {
          refreshAppProfile(account);
          // Release queued dashboard requests only after the backend has
          // set BOTH the signed session and Clerk-bound HttpOnly cookies.
          window.myraaAuthGate?.ready();
        }
      } else {
        await removeBackendSession();
        throw new Error('Clerk-verified account could not access workspace');
      }
    })();
    try { return await exchangeInFlight; }
    finally { exchangeInFlight = null; }
  }
  function refreshAppProfile(user) {
    if (!user) return;
    const avatar=$('workspace-user-profile');
    if(avatar) {const initial=avatar.querySelector('.workspace-avatar-initial'); if(initial)initial.textContent=(user.display_name || 'M').charAt(0).toUpperCase(); else avatar.textContent=(user.display_name || 'M').charAt(0).toUpperCase();avatar.title=`${user.display_name} · ${user.role}`;}
    const name=$('workspace-user-name');if(name)name.textContent=user.display_name||'My account';
    if($('logout-button')) $('logout-button').hidden=false;
    window.dispatchEvent(new CustomEvent('myraa:account', { detail: { name:user.display_name||'My account', email:user.email||'' } }));
    // The full account label lives inside the profile menu, not in the cramped brand breadcrumb.
  }
  async function signOut() {
    window.myraaAuthGate?.fail(new Error('Signed out of MYRAA'));
    await removeBackendSession();
    if(clerk) await clerk.signOut();
    for(const key of ['inventoryAssistantHistory.v1','inventoryAssistantSessions.v1','inventoryAssistantSessionId.v1'])
      try { localStorage.removeItem(key); } catch {}
    location.replace(loginPageUrl);
  }
  async function onClerkChanged() {
    if (!hasSession()) {
      if (loginPage) return;
      await removeBackendSession();
      location.replace(loginPageUrl);
      return;
    }
    if(loginPage) await exchange();
  }
  // Give Clerk its own (flush, not card-in-card) appearance within our MYRAA shell.
  // Official Clerk components continue to handle Google, email OTP, passwords and recovery.
  const loginAppearance = {
    options: {elevation:'flush', socialButtonsPlacement:'top', logoLinkUrl:'/'},
    variables: {
      colorPrimary:'#08765b', colorText:'#173b33', colorTextSecondary:'#5e746c',
      colorBackground:'#ffffff', colorInputBackground:'#ffffff',
      colorInputText:'#173b33', borderRadius:'12px',
      fontFamily:'Inter, Segoe UI, Arial, sans-serif'
    }
  };
  function showLoginHeading(view) {
    const signup = view === 'signup';
    const title=$('clerk-auth-title');
    const subtitle=$('clerk-auth-subtitle');
    const eyebrow=$('clerk-auth-eyebrow');
    if(title) title.textContent = signup ? 'Create your account' : 'Welcome back';
    document.title = signup ? 'MYRAA · Create Account' : 'MYRAA · Sign In';
    if(subtitle) subtitle.textContent = signup
      ? 'Start your private MYRAA workspace with Google or email.'
      : 'Sign in securely with Google, email or password.';
    if(eyebrow) eyebrow.textContent = signup ? 'MYRAA · NEW ACCOUNT' : 'MYRAA · ACCOUNT ACCESS';
    const panel=$('clerk-auth-root');
    if(panel) panel.setAttribute('aria-labelledby',signup?'clerk-tab-signup':'clerk-tab-login');
    for(const [id,selected] of [['clerk-tab-login',!signup],['clerk-tab-signup',signup]]) {
      const tab=$(id);
      if(tab) {
        tab.classList.toggle('active',selected);
        tab.setAttribute('aria-selected',String(selected));
        tab.setAttribute('tabindex',selected?'0':'-1');
      }
    }
  }
  // Clerk uses asynchronous UI rendering. Unmounting one component and mounting
  // another into the SAME node during a tab click can clear the new component
  // after mountSignUp()/mountSignIn() returns, leaving an empty MYRAA card.
  // Navigate to this very same MYRAA page for the alternate mode. The page
  // loads a fresh Clerk instance and mounts exactly one component once.
  function navigateLoginView(requestedView) {
    const view=requestedView === 'signup' ? 'signup' : 'signin';
    if(view===currentLoginView) return;
    const next=new URL(location.href);
    next.pathname=loginPageUrl;
    next.searchParams.set('mode',view);
    next.hash=''; // Discard the previous Clerk hash sub-route.
    location.assign(next.pathname + next.search);
  }
  function showLoadingForm(root,view) {
    root.replaceChildren();
    root.setAttribute('aria-busy','true');
    const pending=document.createElement('div');
    pending.className='clerk-form-loading';
    pending.setAttribute('role','status');
    pending.textContent=`Preparing secure ${view==='signup'?'account creation':'sign-in'}…`;
    root.appendChild(pending);
  }
  // Clerk mounts asynchronously into the auth root. Remove MYRAA's temporary
  // status when the real widget appears, instead of leaving a duplicate line
  // above the form (or hiding Clerk's sign-in / recovery controls).
  function watchClerkForm(root) {
    const mounted=()=>!!root.querySelector('.cl-card, .cl-rootBox, .cl-signIn-root, .cl-signUp-root, form, input, iframe');
    const ready=()=>{
      if (!mounted()) return false;
      root.querySelector('.clerk-form-loading')?.remove();
      root.removeAttribute('aria-busy');
      root.classList.add('clerk-form-ready');
      root.closest('.clerk-auth-card')?.classList.add('has-clerk-form');
      return true;
    };
    const observer=new MutationObserver(()=>{if(ready()) observer.disconnect();});
    observer.observe(root,{childList:true,subtree:true});
    if(ready()) observer.disconnect();
    return observer;
  }
  function showMountWarning(root,view) {
    // An SDK component should produce visible content shortly after mount.
    // If its renderer silently fails, display a retry action instead of leaving
    // an unexplained blank box. Do not collect passwords or bypass Clerk.
    if(currentLoginView!==view || hasSession()) return;
    const rendered=!!root.querySelector('input, [class*="cl-rootBox"], [class*="cl-card"], iframe');
    if(rendered) return;
    statusLine('Secure account form did not load. Check your connection and retry.');
    if(!root.querySelector('.clerk-form-retry')) {
      const retry=document.createElement('button');
      retry.type='button';
      retry.className='clerk-form-retry';
      retry.textContent='Reload secure form';
      retry.addEventListener('click',()=>location.reload());
      root.appendChild(retry);
    }
  }
  async function setLoginView(requestedView) {
    if(!clerk || hasSession()) return;
    const view=requestedView==='signup'?'signup':'signin';
    const root=$('clerk-auth-root');
    if(!root) throw new Error('Authentication container is missing.');
    if(currentLoginView) {
      if(currentLoginView!==view) navigateLoginView(view);
      return;
    }
    showLoginHeading(view);
    showLoadingForm(root,view);
    statusLine('');
    const props={
      routing:'hash',
      signInUrl:localAuthUrl('signin'),
      signUpUrl:localAuthUrl('signup'),
      signInForceRedirectUrl:home,
      signUpForceRedirectUrl:home,
      appearance:loginAppearance
    };
    const formObserver=watchClerkForm(root);
    try {
      if(view==='signup') clerk.mountSignUp(root,props);
      else clerk.mountSignIn(root,props);
      currentLoginView=view;
      // A missing Clerk renderer must not silently appear as a successful form.
      setTimeout(()=>showMountWarning(root,view),12000);
    } catch(error) {
      formObserver.disconnect();
      root.removeAttribute('aria-busy');
      currentLoginView=null;
      statusLine('Unable to display the secure authentication form.');
      throw error;
    }
  }
  async function initLoginPage() {
    $('clerk-tab-login')?.addEventListener('click',()=>navigateLoginView('signin'));
    $('clerk-tab-signup')?.addEventListener('click',()=>navigateLoginView('signup'));
    // Links inside Clerk's widget must also lead to the embedded MYRAA form.
    $('clerk-auth-root')?.addEventListener('click',(event)=>{
      if(event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const link=event.target.closest?.('a[href]');
      if(!link || (link.target && link.target!=='_self')) return;
      let target;
      try {target=new URL(link.href,location.href)} catch {return}
      if(target.origin!==location.origin || target.pathname!==loginPageUrl) return;
      const mode=target.searchParams.get('mode');
      if(mode!=='signin' && mode!=='signup') return;
      event.preventDefault();
      event.stopImmediatePropagation();
      navigateLoginView(mode);
    },true);
    await startClerk();
    clerk.addListener(()=>onClerkChanged().catch(e=>statusLine(e.message)));
    if(hasSession()) await exchange();
    else await setLoginView(new URLSearchParams(location.search).get('mode'));
  }
  async function initAppPage() {
    // No unverified cookie can authenticate the backend. Clerk auth must exist.
    await startClerk();
    if(!hasSession()){
      window.myraaAuthGate?.fail(new Error('No active Clerk session'));
      await removeBackendSession();
      location.replace(loginPageUrl);
      return;
    }
    window.myraaAuthGate?.setRefresh(async () => {
      if(!hasSession()) throw new Error('Clerk session ended');
      return exchange();
    });
    // Bind the backend session to the CURRENT Clerk identity BEFORE unblocking
    // any stock, costing or analytics requests from earlier scripts.
    await exchange();
    const proactivelyRefresh = () => {
      if (!hasSession() || document.visibilityState === 'hidden') return;
      window.myraaAuthGate?.refresh().catch(() => {});
    };
    // Renew ahead of the backend cookie's 15-minute expiry, and when the
    // browser/tab resumes from sleep. Private API requests await refresh.
    setInterval(proactivelyRefresh, 7 * 60 * 1000);
    document.addEventListener('visibilitychange', proactivelyRefresh);
    window.addEventListener('focus', proactivelyRefresh);
    clerk.addListener(({user,session})=>{if(user === null && session === null){
      window.myraaAuthGate?.fail(new Error('Clerk session ended'));
      removeBackendSession().finally(()=>location.replace(loginPageUrl));
    }});
    installProfileAndTeam();
  }
  function installProfileAndTeam() {
    $('logout-button')?.addEventListener('click',e=>{e.preventDefault();signOut().catch(err=>statusLine(err.message));});
    $('workspace-profile-logout')?.addEventListener('click',()=>signOut().catch(err=>alert(err.message)));
    async function openSellerProfile(){
      document.querySelector('.rail-link[data-view="profile"]')?.click();
      try {showProfile(await request('/api/workspace-auth/me'));}catch(e){statusLine(e.message);}
    }
    // Topbar account trigger opens the accessible dropdown; profile link below retains the data loader.
    $('workspace-profile-link')?.addEventListener('click',openSellerProfile);
    $('workspace-profile-close')?.addEventListener('click',()=>document.querySelector('.rail-link[data-view="dashboard"]')?.click());
    $('workspace-profile-form')?.addEventListener('submit',async e=>{
      e.preventDefault();
      const el=$('workspace-profile-message');if(el)el.textContent='Saving profile…';
      try {
        const profile=await request('/api/workspace-auth/me','PATCH',{
          display_name:$('workspace-profile-name').value,business_name:$('workspace-profile-business').value,
          phone:$('workspace-profile-phone').value,address:$('workspace-profile-address').value});
        showProfile(profile);refreshAppProfile(profile);if(el)el.textContent='Profile saved.';
      } catch(err){if(el)el.textContent=err.message;}
    });
    $('workspace-profile-image')?.addEventListener('change',async e=>{
      const file=e.target.files?.[0];if(!file)return;
      const el=$('workspace-profile-message');if(el)el.textContent='Uploading photo…';
      try {const body=new FormData();body.append('file',file);
        const r=await fetch('/api/workspace-auth/me/photo',{method:'POST',body,credentials:'same-origin'});
        const result=await r.json().catch(()=>({}));if(!r.ok)throw new Error(result.detail||`HTTP ${r.status}`);
        showProfile(await request('/api/workspace-auth/me'));if(el)el.textContent='Photo uploaded.';
      }catch(err){if(el)el.textContent=err.message;}finally{e.target.value='';}
    });
  }
  function showProfile(user) {
    for(const [id,value] of Object.entries({
      'workspace-profile-login':user.login_id,'workspace-profile-email':user.email,'workspace-profile-name':user.display_name,
      'workspace-profile-business':user.business_name,'workspace-profile-phone':user.phone,'workspace-profile-address':user.address})) {
      const element=$(id);if(element) element.value=value||'';
    }
    if($('workspace-profile-role')) $('workspace-profile-role').textContent=`Private Seller Workspace · ${user.role}`;
    const photo=$('workspace-profile-preview');if(photo){photo.hidden=!user.photo_url;if(user.photo_url)photo.src=user.photo_url+'?v='+Date.now();}
    const fallback=$('workspace-photo-empty');if(fallback)fallback.hidden=!!user.photo_url;
  }
  const startup=loginPage?initLoginPage:initAppPage;
  startup().catch(error=>{
    statusLine(error.message||'Secure login unavailable');
    if(!loginPage) {
      window.myraaAuthGate?.fail(error);
      location.replace(localAuthUrl('signin'));
    } else {
      const root=$('clerk-auth-root');
      if(root)root.textContent='Unable to initialize Clerk. Check the settings in Railway.';
    }
  });
})();
