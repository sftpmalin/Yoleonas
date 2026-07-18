(function () {
    'use strict';
    if (window.yoleoMobileTablesBooted) return;
    window.yoleoMobileTablesBooted = true;

    const query = '.table-wrap,.desktop-table-wrap,.system-table-wrap,.terminal-table-wrap,.lu-table-wrap,.browser-table-wrap,.docker-stack-table-wrap,.meteo-table-wrap,.yoleo-table-wrap';
    const media = window.matchMedia('(max-width: 900px)');
    let timer = null;
    let modal = null;

    function text(value) {
        return String(value || '').replace(/\s+/g, ' ').trim();
    }

    function cellText(cell) {
        if (!cell) return '';
        const clone = cell.cloneNode(true);
        clone.querySelectorAll('button, input, select, textarea').forEach(control => control.remove());
        return text(clone.innerText || clone.textContent || '');
    }

    function headers(table) {
        const ths = Array.from(table.querySelectorAll('thead th'));
        if (ths.length) return ths.map(cellText);
        const first = table.querySelector('tr');
        return first ? Array.from(first.children).map((_, index) => 'Champ ' + (index + 1)) : [];
    }

    function actionIndex(labels, row) {
        const explicit = labels.findIndex(label => /action|commande|outil|operation|opération/i.test(label || ''));
        if (explicit >= 0) return explicit;
        const cells = Array.from(row.children || []);
        for (let index = cells.length - 1; index >= 0; index -= 1) {
            if (cells[index].querySelector('button, a[href], input[type="submit"], input[type="button"]')) return index;
        }
        return -1;
    }

    function titleIndex(labels, actionCol) {
        const wanted = ['nom', 'name', 'service', 'image', 'partage', 'utilisateur', 'user', 'destination', 'cible', 'pool', 'fichier', 'script'];
        return labels.findIndex((label, index) => index !== actionCol && wanted.some(item => (label || '').toLowerCase().includes(item)));
    }

    function controls(cell) {
        if (!cell) return [];
        return Array.from(cell.querySelectorAll('button, a[href], input[type="submit"], input[type="button"]'))
            .filter(control => !control.closest('.yoleo-mobile-table-grid') && !control.closest('.yoleo-mobile-row-modal'));
    }

    function shouldKeepClassic(table) {
        const editable = table.querySelector('textarea, select, [contenteditable="true"], input:not([type]), input[type="text"], input[type="number"], input[type="search"], input[type="email"], input[type="url"], input[type="password"], input[type="checkbox"], input[type="radio"], input[type="date"], input[type="time"], input[type="datetime-local"], input[type="color"], input[type="file"]');
        return table.classList.contains('fm-table') ||
            table.classList.contains('docker-stack-table') ||
            table.classList.contains('docker-env-table') ||
            !!editable ||
            !!table.closest('.docker-stacks-section, .docker-env-section, .system-info-page, .pers-page, .personnalisation-page, .meteo-page, .confmgr-route-page, .browser-page, .build-database-page, .db-table-wrap, .db-table, .no-mobile-mosaic, .mobile-no-mosaic, [data-mobile-mosaic="off"]');
    }

    function keepClassic(table) {
        const wrap = table.closest(query) || table;
        wrap.classList.remove('yoleo-mobile-source-wrap');
        if (wrap.nextElementSibling && wrap.nextElementSibling.classList.contains('yoleo-mobile-table-grid')) {
            wrap.nextElementSibling.remove();
        }
        delete table.dataset.yoleoMobileSignature;
    }

    function controlLabel(control, index) {
        return text(control.getAttribute('aria-label') || control.getAttribute('title') || control.value || control.textContent || control.name || '') || ('Action ' + (index + 1));
    }

function isMergerfsMobileRow(row) {
        return !!(row && row.closest && (row.dataset?.mobileKind === 'mergerfs' || row.classList.contains('mergerfs-row') || row.closest('#mergerfs-tbody')));
    }

    function mergerfsMobileState(row) {
        if (!isMergerfsMobileRow(row)) return '';
        const datasetState = text(row.dataset?.mobileState || '').toLowerCase();
        if (/running|d[ée]marr[eé]|mounted|mont[eé]|actif|active|ok|succ[èe]s|success/.test(datasetState)) return 'running';
        if (/stopped|arr[êe]t[eé]?|stop|failed|fail|erreur|error|probl[eè]me|problem|d[ée]sactiv[eé]|disabled/.test(datasetState)) return 'stopped';

        // Pour MargeFS, on lit d'abord le badge d'état. Ne jamais se baser
        // sur les boutons d'action de la ligne, sinon le bouton "Arrêter"
        // ferait passer une ligne "démarré" en rouge sur mobile.
        const badgeText = text(row.querySelector('.badge')?.textContent || '').toLowerCase();
        if (/d[ée]marr[eé]|mounted|mont[eé]|actif|active|running|ok|succ[èe]s|success/.test(badgeText)) return 'running';
        if (/arr[êe]t[eé]?|stopped|stop|failed|fail|erreur|error|probl[eè]me|problem|d[ée]sactiv[eé]|disabled/.test(badgeText)) return 'stopped';
        return '';
    }

    function isDiskMobileRow(row) {
        if (isMergerfsMobileRow(row)) return false;
        const table = row && row.closest ? row.closest('table') : null;
        if (!table || !table.closest('.disk-page')) return false;
        if (row.classList.contains('part-row') || row.classList.contains('maintenance-raid-row')) return true;
        if (row.querySelector('.disk-name, .part-name, .mount-col, .status-power')) return true;
        const rowText = text(row.textContent).toLowerCase();
        return /\/dev\/|monté|monte|démonté|demonte|non monté|non monte|standby|veille/.test(rowText);
    }

    function diskMountState(row) {
        if (!isDiskMobileRow(row)) return '';
        const mountText = text(row.querySelector('.mount-col')?.textContent || row.textContent).toLowerCase();
        if (/non monté|non monte|démonté|demonte/.test(mountText)) return 'disk-unmounted';
        if (/monté|monte|\/mnt\/|\/media\/|\/srv\//.test(mountText)) return 'disk-mounted';
        return '';
    }

    function diskPowerState(row) {
        if (!isDiskMobileRow(row)) return '';
        const dot = row.querySelector('.status-dot');
        const stateText = text(row.querySelector('.status-power, .status-cell, .state-col, .badge')?.textContent || row.textContent).toLowerCase();
        if ((dot && dot.classList.contains('dot-standby')) || /standby|veille|sleep|dormi/.test(stateText)) return 'disk-sleeping';
        if ((dot && dot.classList.contains('dot-active')) || /active|actif|running|ok/.test(stateText)) return 'disk-awake';
        return '';
    }

    function iconFor(row, title) {
        if (isDiskMobileRow(row)) return {src: '', text: '', disk: true};
        const img = row.querySelector('img');
        if (img && img.getAttribute('src')) return {src: img.getAttribute('src'), text: ''};
        const status = text(row.querySelector('.status-pill, .badge, .state-pill')?.textContent || '').toLowerCase();
        if (/running|actif|active|ok|d[ée]marr[eé]/.test(status)) return {src: '', text: '✓'};
        if (/stop|stopped|arr[êe]t[eé]?|bad|erreur|error|failed|exited/.test(status)) return {src: '', text: '!'};
        return {src: '', text: text(title).charAt(0).toUpperCase() || '•'};
    }

    function rowState(row) {
        const status = text(row.querySelector('.status-pill, .badge, .state-pill')?.textContent || '').toLowerCase();
        if (/running|actif|active|ok|d[ée]marr[eé]/.test(status)) return 'running';
        if (/stop|stopped|arr[êe]t[eé]?|bad|erreur|error|failed|exited/.test(status)) return 'stopped';
        return '';
    }

    function ensureModal() {
        if (modal) return modal;
        modal = document.createElement('div');
        modal.className = 'yoleo-mobile-row-modal';
        modal.setAttribute('aria-hidden', 'true');
        modal.innerHTML = '<div class="yoleo-mobile-row-backdrop" data-mobile-row-close></div>' +
            '<section class="yoleo-mobile-row-sheet" role="dialog" aria-modal="true" aria-labelledby="yoleoMobileRowTitle">' +
            '<header class="yoleo-mobile-row-head"><span class="yoleo-mobile-tile-icon" id="yoleoMobileRowIcon"></span>' +
            '<div><div class="yoleo-mobile-row-title" id="yoleoMobileRowTitle"></div><div class="yoleo-mobile-row-subtitle" id="yoleoMobileRowSubtitle"></div></div>' +
            '<button type="button" class="yoleo-mobile-row-close" data-mobile-row-close aria-label="Fermer">×</button></header>' +
            '<div class="yoleo-mobile-row-body"><div class="yoleo-mobile-row-details" id="yoleoMobileRowDetails"></div><div class="yoleo-mobile-row-actions" id="yoleoMobileRowActions"></div></div></section>';
        document.body.appendChild(modal);
        modal.addEventListener('click', event => {
            if (event.target.closest('[data-mobile-row-close]')) closeModal();
        });
        return modal;
    }

    function closeModal() {
        if (!modal) return;
        modal.classList.remove('is-open');
        modal.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('yoleo-mobile-row-open');
    }

    function openModal(data) {
        const box = ensureModal();
        const icon = box.querySelector('#yoleoMobileRowIcon');
        const title = box.querySelector('#yoleoMobileRowTitle');
        const subtitle = box.querySelector('#yoleoMobileRowSubtitle');
        const details = box.querySelector('#yoleoMobileRowDetails');
        const actions = box.querySelector('#yoleoMobileRowActions');
        icon.innerHTML = '';
        if (data.icon.src) {
            const img = document.createElement('img');
            img.src = data.icon.src;
            img.alt = '';
            icon.appendChild(img);
        } else if (data.icon.disk) {
            icon.innerHTML = '<span class="yoleo-mobile-disk-icon" aria-hidden="true"></span>';
        } else {
            icon.textContent = data.icon.text || '•';
        }
        title.textContent = data.title || 'Détail';
        subtitle.textContent = data.subtitle || '';
        details.innerHTML = '';
        actions.innerHTML = '';
        data.details.forEach(item => {
            const row = document.createElement('div');
            row.className = 'yoleo-mobile-row-kv';
            row.innerHTML = '<div class="yoleo-mobile-row-k"></div><div class="yoleo-mobile-row-v"></div>';
            row.children[0].textContent = item.key;
            row.children[1].textContent = item.value;
            details.appendChild(row);
        });
        data.controls.forEach((control, index) => {
            const btn = document.createElement('button');
            const label = controlLabel(control, index);
            btn.type = 'button';
            btn.className = 'yoleo-mobile-action-btn';
            btn.textContent = label;
            btn.disabled = !!control.disabled || control.getAttribute('aria-disabled') === 'true';
            const kindText = label + ' ' + (control.value || '');
            if (/supprimer|delete|remove|stop|arrêter|arreter|rm|rmi|danger/i.test(kindText)) btn.classList.add('is-danger');
            else if (/start|démarrer|demarrer|ouvrir|open|edit|modifier|save|sauvegarder/i.test(kindText)) btn.classList.add('is-primary');
            btn.addEventListener('click', () => {
                if (btn.disabled) return;
                closeModal();
                window.setTimeout(() => {
                    if (control.tagName === 'A') {
                        const href = control.getAttribute('href');
                        if (!href) return;
                        if (control.getAttribute('target') === '_blank') window.open(href, '_blank');
                        else window.location.href = href;
                        return;
                    }
                    const type = (control.getAttribute('type') || '').toLowerCase();
                    if ((control.tagName === 'BUTTON' || control.tagName === 'INPUT') && (type === '' || type === 'submit') && control.form && control.form.requestSubmit) {
                        control.form.requestSubmit(control);
                        return;
                    }
                    control.click();
                }, 40);
            });
            actions.appendChild(btn);
        });
        box.classList.add('is-open');
        box.setAttribute('aria-hidden', 'false');
        document.body.classList.add('yoleo-mobile-row-open');
    }

    function build(table) {
        if (!table || table.closest('.yoleo-mobile-row-modal') || table.closest('.yoleo-mobile-table-grid')) return;
        if (shouldKeepClassic(table)) {
            keepClassic(table);
            return;
        }
        const wrap = table.closest(query) || table;
        const rows = Array.from(table.querySelectorAll('tbody tr')).filter(row => {
            const cells = Array.from(row.children || []);
            if (!cells.length || row.classList.contains('group-row')) return false;
            if (cells.length === 1 && cells[0].hasAttribute('colspan')) return false;
            return text(row.textContent).length > 0;
        });
        const labels = headers(table);
        const signature = rows.length + '|' + text(table.textContent).slice(0, 1200);
        let grid = wrap.nextElementSibling && wrap.nextElementSibling.classList.contains('yoleo-mobile-table-grid') ? wrap.nextElementSibling : null;
        if (grid && table.dataset.yoleoMobileSignature === signature) return;
        if (!grid) {
            grid = document.createElement('div');
            grid.className = 'yoleo-mobile-table-grid';
            wrap.insertAdjacentElement('afterend', grid);
        }
        grid.innerHTML = '';
        wrap.classList.add('yoleo-mobile-source-wrap');
        table.dataset.yoleoMobileSignature = signature;
        rows.forEach((row, rowIndex) => {
            const cells = Array.from(row.children || []);
            const actionCol = actionIndex(labels, row);
            const wantedTitle = titleIndex(labels, actionCol);
            const fallbackTitle = cells.findIndex((cell, index) => index !== actionCol && cellText(cell));
            const titleCell = cells[wantedTitle >= 0 ? wantedTitle : fallbackTitle];
            const title = cellText(titleCell) || ('Ligne ' + (rowIndex + 1));
            const subtitleCell = cells.find((cell, index) => index !== actionCol && cell !== titleCell && cellText(cell));
            const subtitle = cellText(subtitleCell);
            const icon = iconFor(row, title);
            const state = mergerfsMobileState(row) || rowState(row);
            const rowControls = controls(cells[actionCol]);
            const details = cells.map((cell, index) => ({key: labels[index] || ('Champ ' + (index + 1)), value: cellText(cell), index}))
                .filter(item => item.index !== actionCol && item.value);
            const tile = document.createElement('button');
            tile.type = 'button';
            tile.className = 'yoleo-mobile-table-tile';
            if (state) tile.classList.add('is-' + state);
            if (isMergerfsMobileRow(row)) {
                tile.classList.add('is-mergerfs-tile');
            }
            if (isDiskMobileRow(row)) {
                tile.classList.add('is-disk-tile');
                const mountState = diskMountState(row);
                const powerState = diskPowerState(row);
                if (mountState) tile.classList.add('is-' + mountState);
                if (powerState) tile.classList.add('is-' + powerState);
            }
            const iconEl = document.createElement('span');
            iconEl.className = 'yoleo-mobile-tile-icon';
            if (icon.src) {
                const img = document.createElement('img');
                img.src = icon.src;
                img.alt = '';
                iconEl.appendChild(img);
            } else if (icon.disk) {
                iconEl.innerHTML = '<span class="yoleo-mobile-disk-icon" aria-hidden="true"></span>';
            } else {
                iconEl.textContent = icon.text || '•';
            }
            const titleEl = document.createElement('span');
            titleEl.className = 'yoleo-mobile-tile-title';
            titleEl.textContent = title;
            const subEl = document.createElement('span');
            subEl.className = 'yoleo-mobile-tile-sub';
            subEl.textContent = subtitle || (rowControls.length ? (rowControls.length + ' action(s)') : 'Détail');
            tile.append(iconEl, titleEl, subEl);
            tile.addEventListener('click', () => openModal({icon, title, subtitle, details, controls: rowControls}));
            grid.appendChild(tile);
        });
    }

    function refresh() {
        document.body.classList.toggle('yoleo-mobile-mosaic-ready', media.matches);
        document.querySelectorAll('table').forEach(build);
    }

    function schedule() {
        window.clearTimeout(timer);
        timer = window.setTimeout(refresh, 80);
    }

    if (media.addEventListener) media.addEventListener('change', schedule);
    else if (media.addListener) media.addListener(schedule);
    document.addEventListener('DOMContentLoaded', schedule);
    window.addEventListener('load', schedule);
    new MutationObserver(mutations => {
        if (mutations.every(item => item.target.closest && item.target.closest('.yoleo-mobile-table-grid, .yoleo-mobile-row-modal'))) return;
        schedule();
    }).observe(document.documentElement, {childList: true, subtree: true});
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') closeModal();
    });
    schedule();
})();
