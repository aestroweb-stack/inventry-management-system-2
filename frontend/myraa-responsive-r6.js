/* R6: label record fields on dynamically rendered tables, without replacing controls. */
(() => {
  'use strict';
  const stage = document.getElementById('stage');
  if (!stage) return;
  const selector = '#view-history table.grid, #view-dashboard table.grid, #view-reorder table.grid, #view-orders table.grid, .invoice-records-table, #myraa-challan-studio table.cs-table, .business-design-table, #view-packing .packing-sample-panel table.grid';
  const wide = /^(when|note|product|product name \/ master sku|design name|supplier|actions|invoice no\.|master sku|sku)$/i;
  function annotate(table) {
    const head = table.tHead?.rows[0];
    if (!head || !table.tBodies.length) return;
    const labels = [...head.cells].map(cell => cell.textContent.trim());
    if (!labels.length) return;
    table.classList.add('myraa-record-table');
    table.setAttribute('role','table');
    table.parentElement.classList.add('myraa-record-scroll');
    table.tHead.setAttribute('role','rowgroup');
    head.setAttribute('role','row');
    for (const cell of head.cells) cell.setAttribute('role','columnheader');
    for (const body of table.tBodies) {
      body.setAttribute('role','rowgroup');
      for (const row of body.rows) {
        row.setAttribute('role','row');
        for (const [i, cell] of [...row.cells].entries()) {
          cell.setAttribute('role','cell');
          if (cell.colSpan > 1) continue;
          const label = labels[i] || (cell.classList.contains('col-img') ? 'Photo' : '');
          if (label) cell.dataset.myraaLabel = label;
          if (wide.test(label)) cell.dataset.myraaWide = '';
        }
      }
    }
  }
  function refresh() { stage.querySelectorAll(selector).forEach(annotate); }
  let queued = false;
  new MutationObserver(records => {
    if (!records.some(r => r.type === 'childList' && (r.addedNodes.length || r.removedNodes.length))) return;
    if (queued) return;
    queued = true;
    queueMicrotask(() => { queued = false; refresh(); });
  }).observe(stage,{childList:true,subtree:true});
  refresh();
})();
