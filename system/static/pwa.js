(() => {
  'use strict';

  if (!window.isSecureContext || !('serviceWorker' in navigator)) return;

  window.addEventListener('load', async () => {
    try {
      // Garder l'URL historique permet de mettre à jour la PWA déjà installée.
      const registration = await navigator.serviceWorker.register('/sw.js', {
        scope: '/',
        updateViaCache: 'none'
      });
      registration.update().catch(() => {});
    } catch (error) {
      console.warn('PWA YoLéo : service worker non enregistré.', error);
    }
  });
})();
