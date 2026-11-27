/* Tenant-private invoice image viewer. No external URLs or HTML embedding. */
(() => {
 'use strict';
 let dialog, url, epoch=0;
 function close(){epoch++;if(dialog?.open)dialog.close();if(url){URL.revokeObjectURL(url);url=null;}}
 function ensure(){if(dialog)return dialog;dialog=document.createElement('dialog');dialog.id='myraa-invoice-photo';dialog.className='ri-photo-dialog myraa-invoice-photo';dialog.innerHTML='<div class="ri-gallery-head"><h3></h3><button type="button" class="cs-secondary">Close</button></div><div class="myraa-invoice-image-scroll"><img class="ri-gallery-main" alt="Invoice image"></div><div class="myraa-invoice-preview-tools"><button type="button" class="cs-secondary" data-image-zoom aria-pressed="false">Actual size</button><a class="cs-secondary" download>Download image</a></div>';dialog.querySelector('button').onclick=close;const zoom=dialog.querySelector('[data-image-zoom]');zoom.onclick=()=>{const active=dialog.classList.toggle('is-actual-size');zoom.setAttribute('aria-pressed',String(active));zoom.textContent=active?'Fit to screen':'Actual size';dialog.querySelector('.myraa-invoice-image-scroll').scrollTo(0,0);};dialog.addEventListener('close',()=>{if(!dialog.open){epoch++;if(url)URL.revokeObjectURL(url);url=null;dialog.querySelector('img').removeAttribute('src');}});document.body.append(dialog);return dialog;}
 async function open(source,title='Invoice image'){
   const seq=++epoch;let blob;
   if(source instanceof Blob)blob=source;
   else {if(typeof source!=='string'||!/^\/api\/(?:purchase-challans\/\d+\/attachments\/\d+|purchase-returns\/(?:drafts\/)?\d+\/documents\/\d+)$/.test(source))throw new Error('Invalid invoice image path');const r=await fetch(source,{credentials:'same-origin',cache:'no-store'});if(!r.ok)throw new Error(`Invoice image unavailable (${r.status})`);blob=await r.blob();}
   if(seq!==epoch)return;if(!/^image\/(jpeg|png|webp)$/.test(blob.type))throw new Error('This attachment is not a supported image. Use its download link.');
   const d=ensure();d.classList.remove('is-actual-size');const zoom=d.querySelector('[data-image-zoom]');zoom.setAttribute('aria-pressed','false');zoom.textContent='Actual size';if(url)URL.revokeObjectURL(url);url=URL.createObjectURL(blob);d.querySelector('h3').textContent=title;d.querySelector('img').src=url;const link=d.querySelector('a');link.href=url;link.download=title;if(!d.open)d.showModal();
 }
 window.MYRAAInvoicePreview={open,close};window.addEventListener('myraa:view',close);window.addEventListener('myraa:account',close);
})();
