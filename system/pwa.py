"""PWA publique de YoLéo, configurée depuis conf/mobile.conf.

Seuls la page hors ligne et ses assets publics sont conservés dans le cache.
Les pages du NAS, les sessions et l'API restent exclusivement sur le réseau.
"""

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, Response, current_app, jsonify, make_response

pwa_bp = Blueprint("pwa_bp", __name__)


@pwa_bp.record_once
def prepare_config(state):
    ensure_mobile_conf_file()


@pwa_bp.app_context_processor
def pwa_context():
    cfg = mobile_conf_read()
    return {"pwa_config": cfg, "pwa_icon_src": mobile_manifest_icons(cfg)[-1]["src"]}


MOBILE_CONF = os.path.join(os.environ.get('NAS_CONF_DIR', str(Path(__file__).resolve().parent.parent / 'conf')), 'mobile.conf')

MOBILE_DEFAULT_CONF = {
    "SHORT_NAME": "Yoleo",
    "NAME": "Yoleo Nas OS",
    "DESCRIPTION": "Interface NAS Debian",
    "START_URL": "/index",
    "DISPLAY": "standalone",
    "BACKGROUND_COLOR": "#000000",
    "THEME_COLOR": "#000000",
    "ICON_SRC": "/static/logo.png",
}

# Tailles carrées dérivées de static/logo.png, sans recadrer le logo fourni.
# Le nom versionné renouvelle aussi l'icône des installations PWA existantes.
PWA_LOGO_ICONS = {
    size: f"/static/pwa-logo-{size}-039c959b3849.png" for size in (192, 512)
}


def mobile_manifest_icons(cfg: dict) -> list[dict]:
    default_logo = cfg["ICON_SRC"] == MOBILE_DEFAULT_CONF["ICON_SRC"]
    return [
        {
            "src": PWA_LOGO_ICONS[size] if default_logo else cfg["ICON_SRC"],
            "sizes": f"{size}x{size}",
            "type": "image/png",
            "purpose": "any" if default_logo else "any maskable",
        }
        for size in (192, 512)
    ]


def _mobile_clean_value(value: str, default: str = "") -> str:
    value = str(value if value is not None else "").strip().replace("\r", " ").replace("\n", " ")
    return value or default


def _mobile_clean_color(value: str, default: str = "#000000") -> str:
    value = _mobile_clean_value(value, default)
    if len(value) == 7 and value.startswith("#"):
        allowed = "0123456789abcdefABCDEF"
        if all(ch in allowed for ch in value[1:]):
            return value.upper()
    return default


def _mobile_clean_static_icon(value: str, default: str = "/static/logo.png") -> str:
    value = _mobile_clean_value(value, default).replace("\\", "/")
    if value.startswith("static/"):
        value = "/" + value
    if not value.startswith("/static/"):
        return default
    if any(part in {".", ".."} for part in value.split("/")) or "%" in value:
        return default
    lower = value.lower()
    if not lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico")):
        return default
    return value


def mobile_conf_normalize(data: dict | None = None) -> dict:
    raw = dict(MOBILE_DEFAULT_CONF)
    if data:
        for key, value in data.items():
            clean_key = str(key or "").strip().upper()
            if clean_key in raw:
                raw[clean_key] = str(value).strip()
    raw["SHORT_NAME"] = _mobile_clean_value(raw.get("SHORT_NAME"), MOBILE_DEFAULT_CONF["SHORT_NAME"])
    raw["NAME"] = _mobile_clean_value(raw.get("NAME"), MOBILE_DEFAULT_CONF["NAME"])
    raw["DESCRIPTION"] = _mobile_clean_value(raw.get("DESCRIPTION"), MOBILE_DEFAULT_CONF["DESCRIPTION"])
    raw["START_URL"] = _mobile_clean_value(raw.get("START_URL"), MOBILE_DEFAULT_CONF["START_URL"])
    start = urlsplit(raw["START_URL"])
    if (not raw["START_URL"].startswith("/") or raw["START_URL"].startswith("//")
            or start.netloc or start.scheme or "\\" in raw["START_URL"]):
        raw["START_URL"] = MOBILE_DEFAULT_CONF["START_URL"]
    raw["DISPLAY"] = raw.get("DISPLAY", MOBILE_DEFAULT_CONF["DISPLAY"]).strip().lower()
    if raw["DISPLAY"] not in {"standalone", "fullscreen", "minimal-ui", "browser"}:
        raw["DISPLAY"] = MOBILE_DEFAULT_CONF["DISPLAY"]
    raw["BACKGROUND_COLOR"] = _mobile_clean_color(raw.get("BACKGROUND_COLOR"), MOBILE_DEFAULT_CONF["BACKGROUND_COLOR"])
    raw["THEME_COLOR"] = _mobile_clean_color(raw.get("THEME_COLOR"), MOBILE_DEFAULT_CONF["THEME_COLOR"])
    raw["ICON_SRC"] = _mobile_clean_static_icon(raw.get("ICON_SRC"), MOBILE_DEFAULT_CONF["ICON_SRC"])
    return raw


def mobile_conf_read(path: str = "") -> dict:
    target = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or MOBILE_CONF).strip())))
    data = {}
    if os.path.exists(target):
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    data[key.strip().upper()] = value.strip()
        except Exception as exc:
            print(f"⚠️ Lecture mobile.conf impossible : {exc}")
    return mobile_conf_normalize(data)


def mobile_conf_write(config: dict, path: str = "") -> str:
    target = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or MOBILE_CONF).strip())))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    clean = mobile_conf_normalize(config)
    lines = [
        "# ============================================================\n",
        "# Configuration mobile / PWA - manifest.json\n",
        "# Fichier créé automatiquement par pwa.py si absent.\n",
        "# Modifiable depuis Système > Personnalisation > Mobile.\n",
        "# ============================================================\n",
        "\n",
    ]
    for key in ["SHORT_NAME", "NAME", "DESCRIPTION", "START_URL", "DISPLAY", "BACKGROUND_COLOR", "THEME_COLOR", "ICON_SRC"]:
        lines.append(f"{key}={clean[key]}\n")
    with open(target, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    try:
        os.chmod(target, 0o644)
    except OSError:
        pass
    return target


def ensure_mobile_conf_file(path: str = "") -> bool:
    target = os.path.abspath(os.path.expanduser(os.path.expandvars(str(path or MOBILE_CONF).strip())))
    if os.path.exists(target):
        return False
    mobile_conf_write(MOBILE_DEFAULT_CONF, target)
    return True


@pwa_bp.get('/manifest.webmanifest')
@pwa_bp.get('/manifest.json')
def manifest():
    cfg = mobile_conf_read()
    data = {
        "short_name": cfg["SHORT_NAME"],
        "name": cfg["NAME"],
        "description": cfg["DESCRIPTION"],
        "start_url": cfg["START_URL"],
        "display": cfg["DISPLAY"],
        "background_color": cfg["BACKGROUND_COLOR"],
        "theme_color": cfg["THEME_COLOR"],
        "icons": mobile_manifest_icons(cfg)
    }
    resp = make_response(jsonify(data))
    resp.headers['Content-Type'] = 'application/manifest+json'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@pwa_bp.get('/offline.html')
def offline():
    # Aucun contexte des modules NAS ni aucune donnée de session dans ce cache.
    html = current_app.jinja_env.get_template('offline.html').render(
        pwa_config=mobile_conf_read()
    )
    response = Response(html, mimetype='text/html')
    response.headers['Cache-Control'] = 'no-cache'
    return response


@pwa_bp.get('/service-worker.js')
@pwa_bp.get('/sw.js')
def sw():
    cfg = mobile_conf_read()
    assets = list(dict.fromkeys([
        '/offline.html', '/static/offline.js', '/static/pwa.js', cfg['ICON_SRC'],
        *(icon['src'] for icon in mobile_manifest_icons(cfg))
    ]))
    # Une personnalisation ou un déploiement renouvelle aussi la page déjà cachée.
    version = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode())
    root = Path(__file__).resolve().parent
    for path in [Path(__file__), root / 'templates/offline.html',
                 *(root / asset.lstrip('/') for asset in assets
                   if asset.startswith('/static/'))]:
        try:
            stat = path.stat()
            version.update(f'{stat.st_mtime_ns}:{stat.st_size}'.encode())
        except OSError:
            version.update(b'missing')
    source = (
        "'use strict';\n"
        f"const CACHE = 'yoleo-pwa-{version.hexdigest()[:16]}';\n"
        f"const PUBLIC_ASSETS = {json.dumps(assets)};\n"
        + SERVICE_WORKER_SOURCE
    )
    response = Response(source, mimetype='application/javascript')
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


SERVICE_WORKER_SOURCE = r"""
const OFFLINE_URL = '/offline.html';

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE)
      .then(cache => cache.addAll(PUBLIC_ASSETS.map(url => new Request(url, {
        cache: 'reload', credentials: 'omit'
      }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys
        .filter(key => key.startsWith('yoleo-pwa-') && key !== CACHE)
        .map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // L'API (même ouverte dans un onglet) ne reçoit jamais de réponse de secours HTML.
  if (url.pathname === '/api' || url.pathname.startsWith('/api/')) return;

  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (response.status < 500) return response;
        // Une panne du serveur peut aussi être signalée par le reverse proxy.
      } catch (error) {
        // Pas de réseau : afficher uniquement la page publique préparée à l'installation.
      }
      const cache = await caches.open(CACHE);
      return cache.match(OFFLINE_URL);
    })());
    return;
  }

  // Liste explicite : aucun fichier privé ni résultat d'API n'est mis en cache.
  if (PUBLIC_ASSETS.includes(url.pathname) && !url.search) {
    event.respondWith(
      caches.open(CACHE).then(cache => cache.match(request))
        .then(cached => cached || fetch(request))
    );
  }
});
"""

