/* MYRAA UI additions: no fake data; interactive file selected labels only. */
(() => {
  'use strict';
  const excel = document.getElementById('invoice-excel-file');
  const label = document.getElementById('invoice-file-name');
  excel?.addEventListener('change', () => {
    if (label) label.textContent = excel.files?.[0]?.name || '.xlsx, .xls or .csv · Max 12 MB';
  });
  const importForm = document.getElementById('challan-import-form');
  importForm?.addEventListener('reset', () => {
    if (label) label.textContent = '.xlsx, .xls or .csv · Max 12 MB';
  });
})();
