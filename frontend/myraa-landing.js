/* MYRAA public product-category preview. No private inventory data is exposed. */
(() => {
  'use strict';
  const buttons = [...document.querySelectorAll('.showcase-category')];
  const visual = document.getElementById('showcase-active-image');
  const heading = document.getElementById('showcase-active-name');
  const caption = document.getElementById('showcase-active-desc');
  if (!buttons.length || !visual || !heading || !caption) return;
  buttons.forEach(button => button.addEventListener('click', () => {
    buttons.forEach(other => {
      const active = other === button;
      other.classList.toggle('is-active', active);
      other.setAttribute('aria-pressed', String(active));
    });
    visual.src = button.dataset.image;
    visual.alt = `Illustrative ${button.dataset.category} catalog example`;
    heading.textContent = button.dataset.category;
    caption.textContent = button.dataset.description;
  }));
})();
