/* MYRAA supplier challans: real CRUD, safe staged receipts, one-time stock push. */
(() => {
'use strict';
const $=id=>document.getElementById(id);
if(!$('myraa-challan-studio'))return;
const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=n=>'₹'+Number(n||0).toLocaleString('en-IN',{maximumFractionDigits:2});
const row=()=>({name:'',sku:'',size:'',qty_ordered:'',qty_received:0,unit_cost:null});
const dateToday=()=>new Date(Date.now()-new Date().getTimezoneOffset()*60000).toISOString().slice(0,10);
const state={id:null,status:'NEW',mode:'edit',invoice_no:'',rows:[],files:[],attachments:[],busy:false};
const msg=(text,bad=false)=>{const el=$('cs-message');el.textContent=text||'';el.classList.toggle('is-error',bad);if(bad&&text)window.MYRAAUI.error(text);};
const api=async(url,options={})=>{
 const response=await fetch('/api/purchase-challans'+url,{credentials:'same-origin',...options});
 const data=await response.json().catch(()=>({}));
 if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:`HTTP ${response.status}`);
 return data;
};
const editable=()=>state.mode!=='view';
function render(){
 const hasRows=state.rows.length>0;
 $('cs-empty').hidden=hasRows;
 $('cs-table-wrap').hidden=!hasRows;
 $('cs-rows').innerHTML=state.rows.map((r,i)=>`<tr data-row="${i}"><td>${i+1}</td><td class="cs-product-cell"><input aria-label="Product name" class="cs-name" data-field="name" value="${escape(r.name)}" maxlength="300" placeholder="Product name" ${editable()?'':'disabled'} required><input aria-label="Master SKU" class="cs-sku" data-field="sku" maxlength="64" value="${escape(r.sku)}" placeholder="Master SKU (optional)" ${editable()?'':'disabled'}></td><td><input aria-label="Size" data-field="size" class="cs-size" maxlength="20" value="${escape(r.size)}" placeholder="S" ${editable()?'':'disabled'} required></td><td><input aria-label="Ordered pcs" data-field="qty_ordered" class="cs-number" type="number" min="1" max="1000000" step="1" value="${escape(r.qty_ordered)}" ${editable()?'':'disabled'} required></td><td><input aria-label="Received pcs" data-field="qty_received" class="cs-number" type="number" min="0" max="1000000" step="1" value="${escape(r.qty_received)}" ${editable()?'':'disabled'} required></td><td class="cs-pending-number">0</td><td><input aria-label="Unit cost" data-field="unit_cost" class="cs-cost" type="number" min="0" step="0.01" value="${r.unit_cost==null?'':escape(r.unit_cost)}" placeholder="0.00" ${editable()?'':'disabled'}></td><td class="cs-row-value">0</td><td><button class="cs-delete" aria-label="Delete item ${i+1}" type="button" ${editable()?'':'disabled'}><i class="fa-solid fa-trash-can"></i></button></td></tr>`).join('');
 $('cs-rows').querySelectorAll('tr').forEach((tr,i)=>{
  if(state.status==='POSTED'){tr.querySelectorAll('[data-field]').forEach(el=>{if(!['qty_ordered','qty_received'].includes(el.dataset.field))el.disabled=true;});tr.querySelector('.cs-delete').disabled=true;}
  tr.querySelectorAll('[data-field]').forEach(el=>el.addEventListener('input',()=>{
   const k=el.dataset.field;state.rows[i][k]=k==='qty_ordered'||k==='qty_received'?Number(el.value):k==='unit_cost'?(el.value===''?null:Number(el.value)):el.value;calc();
  }));
  tr.querySelector('.cs-delete').addEventListener('click',async()=>{
    if(state.busy||!editable()||state.status==='POSTED')return;
    const savedLine=state.rows[i];
    if(state.id && savedLine?.id){
      if(!await window.MYRAAUI.confirm('Permanently delete this saved invoice item? Unsaved changes to other rows will be discarded.',{title:'Delete saved item?',confirmText:'Delete item',variant:'danger'}))return;
      state.busy=true;calc();
      try {
        const updated=await api(`/${state.id}/lines/${savedLine.id}`,{method:'DELETE'});
        select(updated,'edit');
        window.myraaLoadChallans?.().catch(()=>{});
        msg('Saved item deleted. This invoice has not changed inventory stock.');
      }catch(error){msg(error.message,true);}
      finally{state.busy=false;calc();}
    } else {
      state.rows.splice(i,1);render();
      msg('Unsaved item removed.');
    }
  });
 });
 calc();
}
function calc(){
 let ordered=0,received=0,cost=0;
 state.rows.forEach((r,i)=>{
  const o=Number(r.qty_ordered)||0,v=Number(r.qty_received)||0,c=Number(r.unit_cost)||0;
  ordered+=o;received+=v;cost+=v*c;
  const tr=$('cs-rows').querySelectorAll('tr')[i];if(tr){
   const pending=Math.max(0,o-v),el=tr.querySelector('.cs-pending-number');
   el.textContent=String(pending);el.classList.toggle('has-pending',pending>0);
   tr.querySelector('.cs-row-value').textContent=Number(o*c).toLocaleString('en-IN',{maximumFractionDigits:2});
  }
 });
 $('cs-ordered').textContent=ordered.toLocaleString('en-IN')+' pcs';
 $('cs-received').textContent=received.toLocaleString('en-IN')+' pcs';
 $('cs-pending').textContent=Math.max(0,ordered-received).toLocaleString('en-IN')+' pcs';
 $('cs-value').textContent=money(cost);
 const ready=ordered>0 && ordered===received && state.rows.every(r=>r.unit_cost!=null&&r.unit_cost!==''&&Number(r.qty_ordered)>=1);
 $('cs-push').disabled=state.busy||!state.id||!editable()||state.status==='POSTED'||!ready;
 $('cs-push').title=state.status==='POSTED'?'Already posted':!state.id?'Save the invoice first':!ready?'All pieces must be received and costed before push':'Push once to inventory';
 $('cs-add').disabled=state.busy||!editable()||state.status==='POSTED';$('cs-save').disabled=state.busy||!editable()||!state.rows.length;
 $('cs-files').disabled=!editable()||state.status==='POSTED';
}
function renderFiles(){
 const holder=$('cs-previews');holder.replaceChildren();
 const data=[...state.attachments.map(x=>({file:null,filename:x.filename,url:x.url,type:x.content_type,id:x.id})),...state.files.map((x,i)=>({file:x.file,filename:x.file.name,url:x.url,type:x.file.type,pending:i}))];
 for(const item of data){
  const div=document.createElement('div');div.className='cs-file-thumb';
  if(item.type?.startsWith('image/')){const img=document.createElement('img');img.src=item.url;img.alt=item.filename;div.append(img);}
  else {const icon=document.createElement('i');icon.className='fa-solid fa-file-pdf';div.append(icon);}
  const label=document.createElement('small');label.textContent=item.filename;label.title=item.filename;div.append(label);
  if(editable()){
   const close=document.createElement('button');close.type='button';close.textContent='×';close.setAttribute('aria-label','Remove '+item.filename);
   close.addEventListener('click',async()=>{
    try {
     if(item.file){URL.revokeObjectURL(item.url);state.files.splice(item.pending,1);}
     else {if(!await window.MYRAAUI.confirm('Delete this saved invoice scan?',{title:'Remove invoice scan?',confirmText:'Remove scan',variant:'danger'}))return;const response=await fetch(`/api/purchase-challans/${state.id}/attachments/${item.id}`,{method:'DELETE',credentials:'same-origin'});if(!response.ok)throw new Error('Could not delete invoice scan');state.attachments=state.attachments.filter(x=>x.id!==item.id);}
     renderFiles();
    }catch(e){msg(e.message,true);}
   });div.append(close);
  }
  holder.append(div);
 }
}
function clearFiles(){state.files.forEach(x=>URL.revokeObjectURL(x.url));state.files=[];state.attachments=[];renderFiles();}
function reset(){state.id=null;state.status='NEW';state.mode='edit';state.invoice_no='';state.rows=[];clearFiles();for(const id of ['cs-party','cs-number','cs-date'])$(id).disabled=false;$('cs-party').value='';$('cs-number').value='';$('cs-date').value=dateToday();$('cs-export').href='/api/purchase-challans/template';render();$('cs-last-open').hidden=true;msg('New invoice ready. Click Add Item to begin.');window.myraaClearChallanSelection?.();}
function select(data,mode='edit'){state.id=data.id;state.status=data.status;state.mode=mode;state.invoice_no=data.invoice_no||'';state.rows=(data.lines||[]).map(x=>({id:x.id,name:x.name,sku:x.sku,size:x.size,qty_ordered:x.qty_ordered,qty_received:x.qty_received,unit_cost:x.unit_cost}));clearFiles();state.attachments=data.attachments||[];$('cs-party').value=data.party_name;$('cs-number').value=data.challan_no;$('cs-date').value=data.order_date;$('cs-export').href='/api/purchase-challans/'+data.id+'/export';for(const id of ['cs-party','cs-number','cs-date'])$(id).disabled=!editable()||data.status==='POSTED';$('cs-last-open').hidden=true;render();renderFiles();msg(data.status==='POSTED'?'Posted invoice: correct received/ordered quantities and Save. Corrections involving dispatched units are blocked for safety.':mode==='view'?'Viewing saved invoice. Choose Edit in records to make changes.':'Invoice loaded. You can edit its details and save.');}
function payload(){const party=$('cs-party').value.trim(),number=$('cs-number').value.trim(),date=$('cs-date').value;if(!party||!number||!date)throw new Error('Supplier, invoice reference number and date are required.');if(!state.rows.length)throw new Error('Add at least one item.');
const lines=state.rows.map((r,i)=>{
 const name=String(r.name||'').trim(),size=String(r.size||'').trim().toUpperCase(),ordered=Number(r.qty_ordered),received=Number(r.qty_received);
 if(!name||!size||!Number.isInteger(ordered)||ordered<=0||!Number.isInteger(received)||received<0||received>ordered)throw new Error(`Item ${i+1}: fix product name, size, ordered and received quantities.`);
 if(r.unit_cost!==null&&r.unit_cost!==''&&(!Number.isFinite(Number(r.unit_cost))||Number(r.unit_cost)<0))throw new Error(`Item ${i+1}: invalid cost.`);
 return {name,sku:String(r.sku||'').trim(),size,qty_ordered:ordered,qty_received:received,unit_cost:r.unit_cost};
 });return {party_name:party,challan_no:number,order_date:date,invoice_no:state.invoice_no,lines};}
async function uploadQueued(id){if(!state.files.length)return null;const form=new FormData();state.files.forEach(x=>form.append('files',x.file));const response=await fetch(`/api/purchase-challans/${id}/attachments`,{method:'POST',body:form,credentials:'same-origin'});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.detail||`Invoice upload failed: HTTP ${response.status}`);return data;}
async function save({stayOpen=false}={}){
 if(state.busy||!editable())return null;
 let body;
 try{body=payload();}catch(error){msg(error.message,true);throw error;}
 state.busy=true;calc();msg('Saving invoice…');
 try {
  let saved=await api(state.id?`/${state.id}/manual`:'/manual',{
   method:state.id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(state.files.length && state.status!=='POSTED'){
   try{saved=await uploadQueued(saved.id);}
   catch(error){
    // The invoice is saved, but preserve queued files so the seller can retry.
    state.id=saved.id;state.status=saved.status;state.invoice_no=saved.invoice_no||'';
    state.rows=(saved.lines||[]).map(x=>({id:x.id,name:x.name,sku:x.sku,size:x.size,qty_ordered:x.qty_ordered,qty_received:x.qty_received,unit_cost:x.unit_cost}));
    state.attachments=saved.attachments||[];render();renderFiles();
    msg('Invoice saved, but attachments failed: '+error.message+'. Retry Save Invoice.',true);
    window.myraaLoadChallans?.().catch(()=>{});
    return null;
   }
  }
  if(state.status==='POSTED'){select(saved,'edit');msg('Posted invoice quantity correction saved and inventory reconciled.');}
  else if(stayOpen){select(saved,'edit');msg('Invoice saved. Confirm Push Received Stock to update inventory.');}
  else {
   reset();
   const quick=$('cs-last-open');quick.hidden=false;
   quick.dataset.invoiceId=String(saved.id);
   quick.textContent=`Open saved invoice ${saved.challan_no} to push stock / edit`;
   msg(`Invoice ${saved.challan_no} saved. Fresh invoice ready — click Add Item. Inventory has not changed.`);
  }
  window.myraaLoadChallans?.().catch(()=>{});
  return saved;
 }catch(error){msg(error.message,true);throw error;}
 finally{state.busy=false;calc();}
}
$('cs-form').addEventListener('submit',event=>{event.preventDefault();save().catch(()=>{});});
$('cs-last-open').addEventListener('click',()=>{
 const id=Number($('cs-last-open').dataset.invoiceId);
 if(id>0)window.myraaOpenChallan?.(id,'edit');
});
$('cs-new').addEventListener('click',async()=>{if(await window.MYRAAUI.confirm('Start a new invoice? Unsaved changes will be lost.',{title:'Start new invoice?',confirmText:'Start new',variant:'warning'}))reset();});
$('cs-add').addEventListener('click',()=>{if(!editable()||state.status==='POSTED')return;if(state.rows.length>=300){msg('Max 300 items.',true);return;}state.rows.push(row());render();$('cs-rows').lastElementChild?.querySelector('.cs-name')?.focus();});
$('cs-files').addEventListener('change',e=>{const files=Array.from(e.target.files||[]);if(files.length+state.attachments.length+state.files.length>12){msg('Maximum 12 invoice attachments are allowed.',true);return;}for(const file of files){if(file.size>10*1024*1024||!['image/jpeg','image/png','image/webp','application/pdf'].includes(file.type)){msg('PDF/JPG/PNG/WEBP up to 10 MB.',true);continue;}state.files.push({file,url:URL.createObjectURL(file)});}renderFiles();e.target.value='';});
$('cs-push').addEventListener('click',async()=>{if(state.busy||$('cs-push').disabled)return;try{const saved=await save({stayOpen:true});if(!saved||saved.status!=='READY')return;if(!await window.MYRAAUI.confirm('Add all received products to inventory? Each invoice can be posted only once.',{title:'Post received stock?',confirmText:'Post stock',variant:'warning'}))return;state.busy=true;calc();const result=await api(`/${saved.id}/push`,{method:'POST'});select(await api(`/${saved.id}`));window.myraaLoadChallans?.();window.myraaLoadCosting?.();msg(`Success: ${result.received_pieces||saved.received} pieces added to inventory.`);/* Sound is handled once by the successful CRUD request. */}catch(e){msg(e.message,true);window.myraaSound?.error?.();}finally{state.busy=false;calc();}});
$('cs-date').value=dateToday();
window.myraaChallanStudio={onSelected:select,newChallan:reset};
reset();
})();
