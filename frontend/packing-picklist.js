/* MYRAA R8: seller-scoped brand groups, catalog matching and dated snapshots. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const page = $('view-packing'); if (!page || !$('ps-group')) return;
  const base = '/api/packing-sync';
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = n => Number(n || 0).toLocaleString('en-IN');
  const today = () => { const d=new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; };
  const status = $('packing-status'), file=$('packing-file');
  let groups=[], current=null, editId=null, busy=false, loaded=false, refreshing=null, auditPage=1, partyPage=1, historyPage=1, detailToken=0, partyToken=0, historyToken=0;
  let historyRows=new Map();
  $('packing-date').value=today();
  async function api(path, options={}) {
    const r=await fetch(base+path,options);
    if(!r.ok){let message=`HTTP ${r.status}`;try{const x=await r.json();message=typeof x.detail==='string'?x.detail:JSON.stringify(x.detail||message);}catch{}throw new Error(message);}
    return r;
  }
  async function json(path,opts){return (await api(path,opts)).json();}
  function message(text,error=false){status.textContent=text;status.classList.toggle('packing-error',error);}
  function error(e){message(e.message||String(e),true);window.MYRAAUI?.error?.(e.message||String(e));}
  const canonical = x => String(x).split('|')[0].trim();
  const group = () => groups.find(g=>String(g.id)===$('ps-group').value);
  function buttons(){
    $('packing-preview').disabled=busy||!group()||!file.files.length||!$('packing-date').value;
    $('packing-export').disabled=busy||!current;
    $('ps-name').disabled=busy;$('ps-children').disabled=busy;$('ps-manager-close').disabled=busy;$('ps-group-save').disabled=busy; $('ps-group-delete').disabled=busy; $('ps-new').disabled=busy;
    $('ps-group').disabled=busy; $('ps-manage').disabled=busy; $('ps-fallback').disabled=busy;page.querySelectorAll('#ps-group-library button, #ps-file-markets input').forEach(b=>b.disabled=busy);file.disabled=busy;$('packing-date').disabled=busy;$('packing-audit').disabled=busy;
    page.querySelectorAll('.ps-record-actions button').forEach(b=>b.disabled=busy);
  }
  async function work(fn){if(busy)return;busy=true;buttons();try{if(refreshing)await refreshing;await fn();}catch(e){error(e);}finally{busy=false;buttons();}}
  function options(el, items, placeholder, value){el.innerHTML=`<option value="">${esc(placeholder)}</option>`+items.map(([id,name])=>`<option value="${esc(id)}">${esc(name)}</option>`).join(''); if(items.some(([id])=>String(id)===String(value)))el.value=String(value);}
  function renderGroup(){const g=group();$('ps-chips').innerHTML=g?g.children.map(x=>`<span class="ps-chip">${esc(x)}</span>`).join(''):'<span class="hint">No group selected</span>';options($('ps-fallback'),(g?.children||[]).map(x=>[canonical(x),canonical(x)]),'Auto · Brand column / SKU prefix','');buttons();}
  async function loadGroups(preferred){
    const previous=preferred??$('ps-group').value;const hist=$('ps-history-group').value;
    groups=await json('/groups');options($('ps-group'),groups.map(g=>[g.id,g.name]),'Choose Main Brand',previous);
    if(!$('ps-group').value&&groups.length)$('ps-group').value=String(groups[0].id);
    options($('ps-history-group'),groups.map(g=>[g.id,g.name]),'All Main Brands',hist);renderGroup();renderLibrary();
  }
  function renderLibrary(){
    $('ps-group-count').textContent=groups.length;
    $('ps-group-library').innerHTML=groups.map(g=>`<button type="button" class="ps-library-card ${String(g.id)===String(editId)?'is-selected':''}" data-group-id="${g.id}"><span><strong>${esc(g.name)}</strong><small>${g.children.length} source brand${g.children.length===1?'':'s'}</small></span><i class="fa-solid fa-pen" aria-hidden="true"></i></button>`).join('')||'<p class="hint">No saved groups. Create your first Main Brand.</p>';
    buttons();
  }
  function manager(g){
    editId=g?.id??null;$('ps-name').value=g?.name||'';$('ps-children').value=(g?.children||[]).join('\n');
    $('ps-editor-mode').textContent=g?'Editing saved group':'New Main Brand';
    $('ps-editor-hint').textContent=g?'Only this selected group will be updated.':'Create an independent group; existing groups will not change.';
    $('ps-group-save').textContent=g?'Save Changes':'Create Brand Group';$('ps-group-delete').hidden=!g;$('ps-manager').hidden=false;renderLibrary();
  }
  $('ps-group-library').onclick=e=>{const b=e.target.closest('button[data-group-id]');if(!b||busy)return;const g=groups.find(x=>String(x.id)===b.dataset.groupId);if(g)manager(g);};
  const confirm = async (text,opts) => window.MYRAAUI?.confirm ? window.MYRAAUI.confirm(text,opts) : window.confirm(text);
  $('ps-manage').onclick=()=>manager(group());$('ps-new').onclick=()=>manager(null);$('ps-manager-close').onclick=()=>{$('ps-manager').hidden=true;editId=null;};
  $('ps-group-form').onsubmit=e=>{e.preventDefault();work(async()=>{const target=editId;const body={name:$('ps-name').value,children:$('ps-children').value.split(/[\n,]+/).map(x=>x.trim()).filter(Boolean)};const g=await json(target!==null?`/groups/${target}`:'/groups',{method:target!==null?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});await loadGroups(g.id);manager(g);message(target===null?'New brand group created. Existing groups retained.':'Selected brand group updated.');});};
  $('ps-group-delete').onclick=()=>work(async()=>{const target=editId;if(target===null)return;if(!await confirm('Delete this brand group? Saved picklist history and Excel downloads will remain available.',{variant:'danger',title:'Delete brand group?',confirmText:'Delete Group'}))return;await json(`/groups/${target}`,{method:'DELETE'});editId=null;$('ps-manager').hidden=true;await loadGroups();await loadHistory(1);message('Brand group deleted. Saved history retained.');});
  $('ps-group').onchange=()=>{renderGroup();};
  file.onchange=()=>{$('packing-filename').textContent=[...file.files].map(f=>f.name).join(', ')||'.xlsx, .xls, .csv · SKU and Qty';const wrap=$('ps-file-markets');wrap.hidden=!file.files.length;wrap.innerHTML='<strong>Marketplace detection · optional file fallback</strong>'+[...file.files].map((f,i)=>`<label class="ps-file-market"><span>${esc(f.name)}</span><input class="input" data-market-index="${i}" maxlength="80" placeholder="Auto detect · or enter marketplace" aria-label="Marketplace for ${esc(f.name)}"></label>`).join('');buttons();};$('packing-date').onchange=buttons;
  function pager(prefix, data, prev, next, info){$(prefix).hidden=data.pages<=1;$(prev).disabled=data.page<=1;$(next).disabled=data.page>=data.pages;$(info).textContent=`Page ${data.page} of ${data.pages} · ${number(data.count)} rows`;}
  function setCurrent(s){current=s;
    for(const [id,k] of [['packing-source-count','source_rows'],['packing-total','total_qty'],['packing-matched','matched_qty'],['packing-review-count','review_count']])$(id).textContent=s?number(s[k]):'—';
    $('ps-reconcile').hidden=!s;$('ps-reconcile').classList.toggle('ps-warning',!!s?.review_count);
    if(s)$('ps-reconcile').textContent=`${s.group.name} · ${s.date} · ${number(s.matched_qty)} matched + ${number(s.review_qty)} review = ${number(s.total_qty)} pcs · ${number(s.skipped_rows)} total/blank rows ignored`;
    $('packing-party-list').innerHTML=s?Object.entries(s.party_totals||{}).map(([p,n])=>`<div class="ps-party-card"><span><i class="fa-solid fa-boxes-stacked"></i> ${esc(p)}</span><strong>${number(n)} pcs</strong></div>`).join('')||'<p class="hint">No matched packing parties. Check Review.</p>':'<p class="hint">Matched party totals will appear here.</p>';
    $('ps-merged-info').textContent=s?`${number(s.merged_rows)} merged SKU / size rows`:'';
    options($('ps-match-brand'),(s?.group.children||[]).map(x=>[canonical(x),canonical(x)]),'All source brands','');$('ps-match-status').value='all';$('ps-match-q').value='';$('ps-party-q').value='';buttons();
    if(!s){detailToken++;partyToken++;$('packing-sample').innerHTML='<tr><td colspan="7" class="empty">Process a file to review SKU matching.</td></tr>';$('ps-party-items').innerHTML='<tr><td colspan="5" class="empty">No processed items yet.</td></tr>';$('packing-pagination').hidden=true;$('ps-party-pagination').hidden=true;}
  }
  async function loadAudit(p=1){if(!current)return;const token=++detailToken;const qs=new URLSearchParams({view:'audit',status:$('ps-match-status').value,brand:$('ps-match-brand').value,q:$('ps-match-q').value,page:p});const d=await json(`/history/${current.id}?${qs}`);if(token!==detailToken)return;auditPage=d.page;$('packing-sample').innerHTML=d.rows.map(x=>`<tr><td><span class="ps-chip">${esc(x.brand)}</span><small class="ps-issue">${esc(x.marketplace||'Unspecified')}</small></td><td>${esc(x.source_sku)}<small class="ps-issue">${esc(x.file)} · row ${number(x.row)}</small></td><td>${esc(x.product||'—')}</td><td>${esc(x.pick_sku||'—')}</td><td>${esc(x.party||'—')}</td><td>${number(x.qty)}</td><td><span class="ps-state ${x.issue?'ps-review':''}">${x.issue?'Needs review':'Matched'}</span>${x.issue?`<small class="ps-issue">${esc(x.issue)}</small>`:''}</td></tr>`).join('')||'<tr><td colspan="7" class="empty">No rows match this filter.</td></tr>';pager('packing-pagination',d,'packing-prev','packing-next','packing-page-info');}
  async function loadParty(p=1){if(!current)return;const token=++partyToken;const qs=new URLSearchParams({view:'items',q:$('ps-party-q').value,page:p});const d=await json(`/history/${current.id}?${qs}`);if(token!==partyToken)return;partyPage=d.page;$('ps-party-items').innerHTML=d.rows.map(x=>`<tr><td><span class="ps-chip">${esc(x.party)}</span><small class="ps-issue">${esc(x.marketplace||'Unspecified')}</small></td><td><strong>${esc(x.pick_sku)}</strong></td><td>${esc(x.product)}</td><td>${esc(x.brands.join(', '))}</td><td><strong>${number(x.qty)}</strong></td></tr>`).join('')||'<tr><td colspan="5" class="empty">No matching packing rows.</td></tr>';pager('ps-party-pagination',d,'ps-party-prev','ps-party-next','ps-party-page');}
  async function openRun(s){setCurrent(s);await Promise.all([loadAudit(),loadParty()]);}
  async function loadHistory(p=1){const token=++historyToken;const qs=new URLSearchParams({day:$('ps-history-date').value,page:p});if($('ps-history-group').value)qs.set('group_id',$('ps-history-group').value);const d=await json('/history?'+qs);if(token!==historyToken)return;historyPage=d.page;historyRows=new Map(d.items.map(x=>[String(x.id),x]));$('ps-history').innerHTML=d.items.map(x=>`<tr><td>${esc(x.date)}</td><td><span class="ps-chip">${esc(x.group.name)}</span></td><td>${esc(x.group.children.map(canonical).join(', '))}<small class="ps-issue">${esc((x.marketplaces||[]).join(' · ')||'Marketplace unspecified')}</small></td><td>${number(x.source_rows)} rows · ${number(x.total_qty)} pcs<br><span class="ps-state ${x.review_count?'ps-review':''}">${number(x.matched_qty)} matched${x.review_count?` · ${number(x.review_qty)} review pcs`:''}</span></td><td><div class="ps-record-actions"><button type="button" class="btn-secondary" data-ps-action="view" data-id="${x.id}"><i class="fa-solid fa-eye"></i> View</button><button type="button" class="btn-secondary" data-ps-action="download" data-id="${x.id}"><i class="fa-solid fa-download"></i> Download Excel</button><button type="button" class="btn-secondary ps-danger" data-ps-action="delete" data-id="${x.id}" aria-label="Delete saved picklist"><i class="fa-solid fa-trash"></i> Delete</button></div></td></tr>`).join('')||'<tr><td colspan="5" class="empty">No saved picklists for this filter.</td></tr>';pager('ps-history-pagination',d,'ps-history-prev','ps-history-next','ps-history-page');buttons();}
  $('packing-preview').onclick=()=>work(async()=>{const g=group();if(!g||!file.files.length)return;const f=new FormData();for(const selected of file.files)f.append('files',selected);f.append('group_id',g.id);f.append('picklist_date',$('packing-date').value);f.append('default_brand',$('ps-fallback').value);f.append('file_marketplaces',JSON.stringify([...$('ps-file-markets').querySelectorAll('input')].map(x=>x.value.trim())));f.append('include_audit',String($('packing-audit').checked));message('Matching registered Master SKUs and saving picklist…');const s=await json('/process',{method:'POST',body:f});await openRun(s);$('ps-history-date').value='';$('ps-history-group').value='';await loadHistory(1);message(s.review_count?'Saved. Resolve unmatched SKUs in Add Product / aliases and reprocess, or confirm a partial Excel download.':'Saved and quantity verified. Excel ready.');});
  async function download(s){if(!s)return;if(s.review_count&&!await confirm(`${number(s.review_qty)} pieces (${number(s.review_count)} rows) are unmatched and will NOT be packed. They will appear in the Review sheet. Download matched items only?`,{variant:'danger',title:'Download partial picklist?',confirmText:'Download partial Excel'}))return;const r=await api(`/history/${s.id}/excel?allow_partial=${!!s.review_count}`);const blob=await r.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=`MYRAA_Packing_${s.group.name.replace(/[^a-z0-9_-]/gi,'-')}_${s.date}.xlsx`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);}
  $('packing-export').onclick=()=>work(()=>download(current));
  $('ps-history').onclick=e=>{const b=e.target.closest('button[data-ps-action]');if(!b)return;const s=historyRows.get(b.dataset.id);if(!s)return;work(async()=>{if(b.dataset.psAction==='view'){await openRun(s);message(`Opened saved picklist · ${s.date}`);$('packing-sample').closest('section').scrollIntoView({block:'start'});}else if(b.dataset.psAction==='download')await download(s);else if(await confirm(`Remove saved picklist for ${s.group.name} (${s.date}) from your history? Inventory will not change.`,{variant:'danger',title:'Delete saved picklist?',confirmText:'Delete Picklist'})){await json(`/history/${s.id}`,{method:'DELETE'});if(current?.id===s.id)setCurrent(null);await loadHistory(historyPage);message('Picklist removed from history.');}});};
  const safe=fn=>()=>fn().catch(error);
  $('packing-prev').onclick=safe(()=>loadAudit(auditPage-1));$('packing-next').onclick=safe(()=>loadAudit(auditPage+1));$('ps-party-prev').onclick=safe(()=>loadParty(partyPage-1));$('ps-party-next').onclick=safe(()=>loadParty(partyPage+1));$('ps-history-prev').onclick=safe(()=>loadHistory(historyPage-1));$('ps-history-next').onclick=safe(()=>loadHistory(historyPage+1));
  $('ps-match-status').onchange=$('ps-match-brand').onchange=safe(()=>loadAudit(1));$('ps-history-date').onchange=$('ps-history-group').onchange=safe(()=>loadHistory(1));$('ps-history-clear').onclick=safe(()=>{$('ps-history-date').value='';$('ps-history-group').value='';return loadHistory(1);});
  function debounce(fn){let timer;return()=>{clearTimeout(timer);timer=setTimeout(safe(fn),250);};}$('ps-match-q').oninput=debounce(()=>loadAudit(1));$('ps-party-q').oninput=debounce(()=>loadParty(1));
  async function refresh(){if(busy)return;if(refreshing)return refreshing;refreshing=(async()=>{try{await loadGroups();await loadHistory(1);loaded=true;message('Select a Main Brand and upload order files, or open saved history.');}catch(e){loaded=false;error(e);}finally{refreshing=null;}})();return refreshing;}
  window.addEventListener('myraa:view',e=>{if(e.detail==='packing')refresh();});
  window.addEventListener('myraa:account',()=>{if(page.classList.contains('is-active')&&!loaded)refresh();});
  if(page.classList.contains('is-active'))refresh();buttons();
})();
