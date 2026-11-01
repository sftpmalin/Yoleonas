(() => {
    'use strict';
    const page = document.getElementById('qrCodePage');
    if (!page) return;
    const form = document.getElementById('qrCodeForm');
    const text = document.getElementById('qrCodeText');
    const correction = document.getElementById('qrCodeCorrection');
    const generate = document.getElementById('qrCodeGenerate');
    const serverButton = document.getElementById('qrCodeServer');
    const clientButton = document.getElementById('qrCodeClient');
    const clientFile = document.getElementById('qrCodeClientFile');
    const image = document.getElementById('qrCodeImage');
    const placeholder = document.getElementById('qrCodePlaceholder');
    const size = document.getElementById('qrCodeSize');
    const pngButton = document.getElementById('qrCodePng');
    const svgButton = document.getElementById('qrCodeSvg');
    const status = document.getElementById('qrCodeStatus');
    const error = document.getElementById('qrCodeError');
    let generated = null;
    let previewUrl = null;
    let generationId = 0;
    let controller = null;
    let saving = false;
    let browserPopup = null;
    const browserTarget = 'qrCodeServerImport';

    function setDownloads() {
        pngButton.disabled = !generated || saving;
        svgButton.disabled = !generated || saving;
        size.disabled = saving;
    }

    function clearError() {
        error.hidden = true;
        error.textContent = '';
    }

    function showError(message) {
        error.textContent = message;
        error.hidden = false;
        status.textContent = '';
    }

    function clearPreview() {
        image.hidden = true;
        image.removeAttribute('src');
        placeholder.hidden = false;
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = null;
        generated = null;
        setDownloads();
    }

    function invalidate() {
        generationId += 1;
        if (controller) controller.abort();
        controller = null;
        generate.disabled = false;
        generate.textContent = 'Générer';
        clearPreview();
        clearError();
        status.textContent = 'Prêt à générer.';
    }

    function pngBlob(base64) {
        const bytes = Uint8Array.from(atob(base64), character => character.charCodeAt(0));
        return new Blob([bytes], {type: 'image/png'});
    }

    function svgBlob(svg, pixels) {
        const documentSvg = new DOMParser().parseFromString(svg, 'image/svg+xml');
        const root = documentSvg.documentElement;
        if (root.localName !== 'svg') throw new Error('Le fichier SVG est invalide.');
        root.setAttribute('width', String(pixels));
        root.setAttribute('height', String(pixels));
        return new Blob([new XMLSerializer().serializeToString(documentSvg)], {type: 'image/svg+xml'});
    }

    text.addEventListener('input', invalidate);
    correction.addEventListener('change', invalidate);

    async function generateQr(url, options, imported = false) {
        const current = ++generationId;
        if (controller) controller.abort();
        controller = new AbortController();
        const signal = controller.signal;
        clearPreview();
        clearError();
        generate.disabled = true;
        generate.textContent = imported ? 'Chargement…' : 'Génération…';
        status.textContent = imported ? 'Lecture du fichier et vérification du QR code…' : 'Génération du QR code…';
        try {
            const response = await fetch(url, {
                method: 'POST',
                credentials: 'same-origin',
                cache: 'no-store',
                ...options,
                signal,
            });
            if (response.redirected && new URL(response.url).pathname === '/login') {
                throw new Error('Ta session a expiré. Reconnecte-toi à Yoleo pour générer le QR code.');
            }
            if (!(response.headers.get('Content-Type') || '').includes('application/json')) {
                throw new Error('Le serveur ne peut pas générer le QR code pour le moment. Réessaie.');
            }
            const payload = await response.json();
            if (current !== generationId) return;
            if (!response.ok || !payload.ok) {
                throw new Error(payload.error || 'Impossible de générer ce QR code.');
            }
            const png = {};
            for (const pixels of [256, 512, 1024]) png[pixels] = pngBlob(payload.png[pixels]);
            if (imported) text.value = payload.text;
            generated = {svg: payload.svg, png};
            previewUrl = URL.createObjectURL(svgBlob(payload.svg, 512));
            image.src = previewUrl;
            image.hidden = false;
            placeholder.hidden = true;
            status.textContent = 'QR code prêt à télécharger.';
            setDownloads();
        } catch (failure) {
            if (current !== generationId || failure.name === 'AbortError') return;
            clearPreview();
            showError(failure instanceof TypeError
                ? 'Connexion au serveur interrompue. Réessaie.'
                : failure.message || 'Impossible de générer ce QR code.');
        } finally {
            if (current === generationId) {
                controller = null;
                generate.disabled = false;
                generate.textContent = 'Générer';
            }
        }
    }

    form.addEventListener('submit', event => {
        event.preventDefault();
        generateQr(page.dataset.generateUrl, {
            headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
            body: JSON.stringify({text: text.value, correction: correction.value}),
        });
    });

    clientButton.addEventListener('click', () => {
        clientFile.value = '';
        clientFile.click();
    });
    clientFile.addEventListener('change', () => {
        const file = clientFile.files[0];
        if (!file) return;
        if (file.size > Number(page.dataset.maxFileBytes)) {
            invalidate();
            showError('Ce fichier est trop volumineux pour un seul QR code.');
            return;
        }
        const body = new FormData();
        body.append('file', file);
        body.append('correction', correction.value);
        generateQr(page.dataset.importUrl, {headers: {'Accept': 'application/json'}, body}, true);
    });

    serverButton.addEventListener('click', () => {
        const params = new URLSearchParams({target: browserTarget, path: '/mnt/user'});
        browserPopup = window.open(
            page.dataset.browserUrl + '?' + params.toString(),
            'yoleo_qr_code_server_browser',
            'popup=yes,width=1100,height=760,menubar=no,toolbar=no,location=no,status=no,resizable=yes,scrollbars=yes'
        );
        if (browserPopup) browserPopup.focus();
        else showError('La fenêtre de parcours a été bloquée. Autorise les fenêtres pour Yoleo puis réessaie.');
    });
    window.addEventListener('message', event => {
        if (event.origin !== window.location.origin || !browserPopup || event.source !== browserPopup) return;
        const data = event.data || {};
        if (data.type !== 'yoleo-browser-select' || data.mode !== 'file' ||
            data.target !== browserTarget || typeof data.path !== 'string') return;
        browserPopup = null;
        generateQr(page.dataset.importUrl, {
            headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
            body: JSON.stringify({path: data.path, correction: correction.value}),
        }, true);
    });

    async function download(format) {
        if (!generated || saving) return;
        const pixels = Number(size.value);
        if (![256, 512, 1024].includes(pixels)) return;
        const blob = format === 'png' ? generated.png[pixels] : svgBlob(generated.svg, pixels);
        const filename = `qr-code-${pixels}.${format}`;
        saving = true;
        clearError();
        setDownloads();
        try {
            if (typeof window.showSaveFilePicker === 'function' && window.isSecureContext) {
                // All bytes are already available: open the picker directly from
                // the click so browsers keep the required user activation.
                const handle = await window.showSaveFilePicker({
                    suggestedName: filename,
                    excludeAcceptAllOption: true,
                    types: [{description: format === 'png' ? 'Image PNG' : 'Image vectorielle SVG',
                        accept: {[blob.type]: [`.${format}`]}}],
                });
                const writable = await handle.createWritable();
                try {
                    await writable.write(blob);
                    await writable.close();
                } catch (failure) {
                    await writable.abort().catch(() => {});
                    throw failure;
                }
                status.textContent = 'QR code enregistré.';
            } else {
                const url = URL.createObjectURL(blob);
                const link = document.createElement('a');
                link.href = url;
                link.download = filename;
                link.hidden = true;
                document.body.appendChild(link);
                link.click();
                link.remove();
                setTimeout(() => URL.revokeObjectURL(url), 10000);
                status.textContent = 'Téléchargement lancé.';
            }
        } catch (failure) {
            if (failure.name === 'AbortError') status.textContent = 'Enregistrement annulé.';
            else showError('Le fichier n’a pas pu être enregistré. Réessaie le téléchargement.');
        } finally {
            saving = false;
            setDownloads();
        }
    }

    pngButton.addEventListener('click', () => download('png'));
    svgButton.addEventListener('click', () => download('svg'));
    window.addEventListener('pagehide', () => {
        if (controller) controller.abort();
        if (previewUrl) URL.revokeObjectURL(previewUrl);
    });
})();
