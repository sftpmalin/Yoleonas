(() => {
  'use strict';

  const button = document.querySelector('[data-retry]');
  if (!button) return;

  const retry = () => {
    if (location.pathname === '/offline.html') {
      location.replace(button.dataset.startUrl);
    } else {
      location.reload();
    }
  };

  button.addEventListener('click', retry);
  window.addEventListener('online', retry);
})();
