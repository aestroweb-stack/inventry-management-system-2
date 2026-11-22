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
    setTimeout(refresh, 0);
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


/* Invoice scan photos / PDF picker. Keep the existing invoice_images FormData field. */
(() => {
  'use strict';
  const picker = document.getElementById('invoice-scan-picker');
  const input = document.getElementById('invoice-scan-files');
  const zone = document.getElementById('invoice-scan-dropzone');
  const summary = document.getElementById('invoice-scan-summary');
  const previews = document.getElementById('invoice-scan-previews');
  const form = document.getElementById('challan-import-form');
  if (!picker || !input || !zone || !summary || !previews) return;
  const MAX_FILES = 12;
  const MAX_SIZE = 10 * 1024 * 1024; // Consistent with the backend scan limit.
  const permitted = /\.(jpe?g|png|webp|pdf)$/i;
  const urls = [];
  const readableSize = bytes => bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
  const clearUrls = () => { urls.splice(0).forEach(url => URL.revokeObjectURL(url)); };
  const setError = message => {
    input.setCustomValidity(message);
    summary.textContent = message;
    summary.classList.add('is-error');
    zone.classList.add('is-invalid');
  };
  const isAllowed = file => permitted.test(file.name) &&
    (file.type === '' || /^(image\/(jpeg|png|webp)|application\/pdf)$/.test(file.type));

  function render() {
    clearUrls();
    previews.replaceChildren();
    summary.classList.remove('is-error');
    zone.classList.remove('is-invalid');
    input.setCustomValidity('');
    const files = Array.from(input.files || []);
    if (!files.length) {
      summary.textContent = 'No attachments selected · Up to 12 photos/PDFs, 10 MB each';
      return;
    }
    if (files.length > MAX_FILES) { setError(`Maximum ${MAX_FILES} scans allowed. Choose fewer files.`); return; }
    if (files.some(file => !isAllowed(file) || !file.size || file.size > MAX_SIZE)) {
      setError('Only JPG, PNG, WEBP or PDF files up to 10 MB each are allowed.');
      return;
    }
    summary.textContent = `${files.length} file${files.length === 1 ? '' : 's'} selected · Previews shown below`;
    for (const [index, file] of files.entries()) {
      const card = document.createElement('figure');
      card.className = 'invoice-scan-preview';
      const isPdf = file.type === 'application/pdf' || /\.pdf$/i.test(file.name);
      if (isPdf) {
        const pdf = document.createElement('span');
        pdf.className = 'invoice-scan-pdf';
        const symbol = document.createElement('i');
        symbol.className = 'fa-solid fa-file-pdf';
        symbol.setAttribute('aria-hidden', 'true');
        pdf.append(symbol); card.append(pdf);
      } else {
        const thumb = document.createElement('img');
        thumb.className = 'invoice-scan-thumbnail';
        thumb.alt = `Preview of ${file.name}`;
        const url = URL.createObjectURL(file);
        urls.push(url); thumb.src = url;
        card.append(thumb);
      }
      const caption = document.createElement('figcaption');
      const name = document.createElement('span');
      name.className = 'invoice-scan-name';
      name.textContent = file.name;
      name.title = file.name;
      const size = document.createElement('span');
      size.className = 'invoice-scan-size';
      size.textContent = readableSize(file.size);
      caption.append(name, size); card.append(caption);
      const remove = document.createElement('button');
      remove.className = 'invoice-scan-remove';
      remove.type = 'button';
      remove.title = `Remove ${file.name}`;
      remove.setAttribute('aria-label', `Remove ${file.name}`);
      remove.innerHTML = '<i class="fa-solid fa-xmark" aria-hidden="true"></i>';
      remove.addEventListener('click', () => {
        const transfer = new DataTransfer();
        files.forEach((item, position) => { if (position !== index) transfer.items.add(item); });
        input.files = transfer.files;
        render();
      });
      card.append(remove);
      previews.append(card);
    }
  }
  input.addEventListener('change', render);
  form?.addEventListener('reset', () => setTimeout(render, 0));
  zone.addEventListener('dragover', event => { event.preventDefault(); zone.classList.add('is-dragging'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('is-dragging'));
  zone.addEventListener('drop', event => {
    event.preventDefault();
    zone.classList.remove('is-dragging');
    if (!event.dataTransfer?.files?.length) return;
    const transfer = new DataTransfer();
    Array.from(event.dataTransfer.files).forEach(file => transfer.items.add(file));
    input.files = transfer.files;
    render();
  });
  window.addEventListener('pagehide', clearUrls, {once:true});
  render();
})();
