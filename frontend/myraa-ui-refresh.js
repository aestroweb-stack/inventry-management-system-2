/* MYRAA invoice import file-picker: one input, accessible visual state. */
(() => {
  'use strict';
  const excel = document.getElementById('invoice-excel-file');
  const zone = document.getElementById('invoice-file-zone');
  const label = document.getElementById('invoice-file-name');
  const importForm = document.getElementById('challan-import-form');
  if (!excel || !zone || !label) return;
  const defaultText = '.xlsx, .xls or .csv · Max 12 MB';
  const maxSize = 12 * 1024 * 1024;
  const allowed = /\.(xlsx|xls|csv)$/i;
  const refresh = () => {
    const file = excel.files?.[0];
    zone.classList.remove('is-invalid');
    if (!file) {
      label.textContent = defaultText;
      zone.classList.remove('has-file');
      excel.setCustomValidity('');
      return;
    }
    const valid = allowed.test(file.name) && file.size > 0 && file.size <= maxSize;
    zone.classList.toggle('has-file', valid);
    zone.classList.toggle('is-invalid', !valid);
    excel.setCustomValidity(valid ? '' : 'Choose a non-empty XLSX, XLS or CSV under 12 MB');
    label.textContent = valid ? `${file.name} · ${(file.size / 1024).toFixed(1)} KB selected`
      : 'Invalid file: choose XLSX, XLS or CSV (maximum 12 MB)';
  };
  excel.addEventListener('change', refresh);
  importForm?.addEventListener('reset', () => {
    // The form control resets after the reset event dispatch.
    queueMicrotask(refresh);
  });
  zone.addEventListener('dragover', event => {
    event.preventDefault();
    zone.classList.add('is-dragging');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('is-dragging'));
  zone.addEventListener('drop', event => {
    event.preventDefault();
    zone.classList.remove('is-dragging');
    const file = event.dataTransfer?.files?.[0];
    if (!file) return;
    try {
      const transfer = new DataTransfer();
      transfer.items.add(file);
      excel.files = transfer.files;
      refresh();
    } catch {
      // On unsupported browsers users can use the native Browse files picker.
      label.textContent = 'Drag/drop is unavailable here; click Browse files instead';
    }
  });
})();
