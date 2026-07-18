#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Module météo Yoleo.

Conversion Flask du prototype PHP placé dans meteo/index.php.
Le module garde une configuration simple :
  - ../conf/meteo.conf      : chemin du JDOM météo
  - ../conf/meteo.json      : liste des villes
  - ../conf/meteo-top.conf   : ville affichée dans le bandeau haut
  - ../conf/meteo-cache.conf : dernier cache météo utilisable si l’API tombe
  - ../conf/cache/           : caches météo temporaires
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
import threading
import time
from datetime import datetime, date
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


meteo_bp = Blueprint("meteo_bp", __name__)

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(MODULE_DIR, ".."))
TIMEZONE_APP = "Europe/Paris"
DEFAULT_CITY = {"nom": "Paris", "lat": 48.8566, "lng": 2.3522}

# Chemins volontairement simples et fixes, comme demandé.
METEO_CONF_REL = "../conf/meteo.conf"
METEO_JDOM_REL = "../conf/meteo.json"
METEO_TOP_CONF_REL = "../conf/meteo-top.conf"
METEO_PAGE_CACHE_REL = "../conf/meteo-cache.conf"
METEO_CACHE_REL = "../conf/cache/meteo-weather-cache.json"
METEO_TOP_CACHE_REL = "../conf/cache/meteo-top-cache.json"

DEFAULT_METEO_CONF = {
    "JDOM_PATH": METEO_JDOM_REL,
}
DEFAULT_TOP_CONF = {
    "ENABLED": "0",
    "CITY_NAME": "",
    "LAT": "",
    "LNG": "",
}

METEO_TOP_CONTEXT_LOCK = threading.RLock()
METEO_TOP_CONTEXT_CACHE: dict[str, Any] | None = None
METEO_TOP_CONTEXT_CACHE_LOADED_AT = 0.0
METEO_TOP_CONTEXT_SOURCE_MTIME = 0.0
# Petit garde-fou disque : on ne regarde les mtimes que de temps en temps.
# L'actualisation réelle peut être pilotée par le gestionnaire de tâches Linux
# via `python3 meteo.py --update`, sans boucle Python permanente.
METEO_TOP_CONTEXT_DISK_CHECK_TTL = 60


# ---------------------------------------------------------------------------
# Chemins / conf
# ---------------------------------------------------------------------------
def _rel_path(path: str) -> str:
    raw = os.path.expanduser(os.path.expandvars(str(path or "").strip()))
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    return os.path.abspath(os.path.join(MODULE_DIR, raw))


def meteo_conf_path() -> str:
    return _rel_path(METEO_CONF_REL)


def meteo_top_conf_path() -> str:
    return _rel_path(METEO_TOP_CONF_REL)


def meteo_page_cache_path() -> str:
    return _rel_path(METEO_PAGE_CACHE_REL)


def meteo_cache_path() -> str:
    return _rel_path(METEO_CACHE_REL)


def meteo_top_cache_path() -> str:
    return _rel_path(METEO_TOP_CACHE_REL)


def _read_kv(path: str) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                data[key.strip().upper()] = value.strip()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return data


def _write_kv(path: str, rows: dict[str, Any], header: str = "") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        if header:
            for line in header.rstrip().splitlines():
                handle.write(f"# {line}\n")
        for key, value in rows.items():
            handle.write(f"{key}={value if value is not None else ''}\n")
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    return path


def ensure_meteo_conf() -> str:
    path = meteo_conf_path()
    if not os.path.exists(path):
        _write_kv(path, DEFAULT_METEO_CONF, "Configuration météo Yoleo\nJDOM_PATH pointe vers la liste JSON des villes.")
    ensure_meteo_jdom()
    if not os.path.exists(meteo_top_conf_path()):
        write_top_conf(None)
    return path


def read_meteo_conf() -> dict[str, str]:
    ensure_meteo_conf()
    conf = DEFAULT_METEO_CONF.copy()
    conf.update(_read_kv(meteo_conf_path()))
    if not conf.get("JDOM_PATH"):
        conf["JDOM_PATH"] = METEO_JDOM_REL
    return conf


def meteo_jdom_path() -> str:
    conf = DEFAULT_METEO_CONF.copy()
    if os.path.exists(meteo_conf_path()):
        conf.update(_read_kv(meteo_conf_path()))
    return _rel_path(conf.get("JDOM_PATH") or METEO_JDOM_REL)


def _bundled_cities_path() -> str:
    return os.path.join(MODULE_DIR, "meteo", "meteo.json")


def ensure_meteo_jdom() -> str:
    path = meteo_jdom_path()
    if os.path.exists(path):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    source = _bundled_cities_path()
    if os.path.exists(source):
        try:
            with open(source, "r", encoding="utf-8", errors="replace") as src:
                data = json.load(src)
            save_cities(data, path)
            return path
        except Exception:
            pass
    save_cities([DEFAULT_CITY], path)
    return path


def _normalize_city(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    nom = str(raw.get("nom") or raw.get("name") or "").strip()
    if not nom:
        return None
    try:
        lat = float(raw.get("lat", raw.get("latitude")))
        lng = float(raw.get("lng", raw.get("lon", raw.get("longitude"))))
    except Exception:
        return None
    return {"nom": nom, "lat": lat, "lng": lng}


def load_cities() -> list[dict[str, Any]]:
    path = ensure_meteo_jdom()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            decoded = json.load(handle)
    except Exception:
        decoded = []
    cities = [_normalize_city(item) for item in decoded if isinstance(decoded, list)] if isinstance(decoded, list) else []
    cities = [item for item in cities if item]
    if not cities:
        cities = [DEFAULT_CITY.copy()]
        save_cities(cities, path)
    return cities


def save_cities(cities: list[dict[str, Any]], path: str | None = None) -> str:
    clean = [_normalize_city(item) for item in (cities or [])]
    clean = [item for item in clean if item]
    if not clean:
        clean = [DEFAULT_CITY.copy()]
    target = path or meteo_jdom_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(clean, handle, ensure_ascii=False, indent=4)
        handle.write("\n")
    try:
        os.chmod(target, 0o644)
    except OSError:
        pass
    return target


def read_top_conf() -> dict[str, str]:
    ensure_meteo_conf()
    conf = DEFAULT_TOP_CONF.copy()
    conf.update(_read_kv(meteo_top_conf_path()))
    conf["ENABLED"] = "1" if str(conf.get("ENABLED") or "").strip().lower() in {"1", "true", "yes", "on", "oui"} else "0"
    return conf


def write_top_conf(city: dict[str, Any] | None) -> str:
    if city:
        rows = {
            "ENABLED": "1",
            "CITY_NAME": str(city.get("nom") or ""),
            "LAT": str(city.get("lat") or ""),
            "LNG": str(city.get("lng") or ""),
        }
    else:
        rows = DEFAULT_TOP_CONF.copy()
    return _write_kv(meteo_top_conf_path(), rows, "Météo affichée dans le bandeau haut.\nENABLED=0 désactive l'affichage dans menu.html.")


# ---------------------------------------------------------------------------
# API météo
# ---------------------------------------------------------------------------
def _tz_now() -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(TIMEZONE_APP))
        except Exception:
            pass
    return datetime.now()


def http_get_json(url: str, timeout: float = 4.0, retries: int = 0, retry_delay: float = 0.6) -> dict[str, Any]:
    attempts = max(1, int(retries) + 1)
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            req = Request(url, headers={"Accept": "application/json", "User-Agent": "Yoleo-Meteo/1.0"})
            with urlopen(req, timeout=timeout) as response:  # noqa: S310 - URL construite vers API météo publique
                raw = response.read().decode("utf-8", errors="replace")
            decoded = json.loads(raw)
            if not isinstance(decoded, dict):
                raise RuntimeError("Réponse JSON invalide")
            return decoded
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts - 1:
                break
            time.sleep(max(0.0, retry_delay) * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Réponse JSON invalide")


def get_icon(code: Any) -> str:
    try:
        value = int(code)
    except Exception:
        return "❓"
    if value == 0:
        return "☀️"
    if value <= 3:
        return "⛅"
    if value <= 48:
        return "🌫️"
    if value <= 55:
        return "🌦️"
    if value <= 65:
        return "🌧️"
    if value <= 75:
        return "❄️"
    if value <= 82:
        return "⛈️"
    return "⛈️"


def get_sun_times(day: date, lat: float, lng: float) -> dict[str, str]:
    try:
        day_of_year = int(day.strftime("%j"))
        b = (2 * math.pi / 365) * (day_of_year - 81)
        equation_of_time = 9.87 * math.sin(b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)
        declination = 23.45 * math.sin((2 * math.pi / 365) * (day_of_year - 81))

        def hour_angle(angle_deg: float) -> float:
            rad_angle = math.radians(angle_deg)
            rad_lat = math.radians(lat)
            rad_decl = math.radians(declination)
            numerator = -math.sin(rad_angle) - math.sin(rad_lat) * math.sin(rad_decl)
            denominator = math.cos(rad_lat) * math.cos(rad_decl)
            if denominator == 0.0:
                return 0.0
            val = max(-1.0, min(1.0, numerator / denominator))
            return math.degrees(math.acos(val))

        ha_sun = hour_angle(0.833)
        ha_civil = hour_angle(6.0)
        solar_noon = 720 - (4 * lng) - equation_of_time
        offset = 120 if 3 < int(day.strftime("%m")) < 10 else 60

        def min_to_hm(minutes: float) -> str:
            total = (minutes + offset + 1440.0) % 1440.0
            hours = int(total // 60)
            mins = int(total % 60)
            return f"{hours:02d}:{mins:02d}"

        return {
            "aube": min_to_hm(solar_noon - (ha_civil * 4)),
            "lever": min_to_hm(solar_noon - (ha_sun * 4)),
            "coucher": min_to_hm(solar_noon + (ha_sun * 4)),
            "fin": min_to_hm(solar_noon + (ha_civil * 4)),
        }
    except Exception:
        return {"aube": "--", "lever": "--", "coucher": "--", "fin": "--"}


def fetch_current_weather(lat: float, lng: float, timeout: float = 3.0, retries: int = 0) -> dict[str, Any]:
    query = urlencode({
        "latitude": lat,
        "longitude": lng,
        "current_weather": "true",
        "timezone": TIMEZONE_APP,
    })
    data = http_get_json(f"https://api.open-meteo.com/v1/forecast?{query}", timeout=timeout, retries=retries)
    current = data.get("current_weather")
    return current if isinstance(current, dict) else {}


def fetch_forecast(lat: float, lng: float, timeout: float = 5.0, retries: int = 0) -> dict[str, Any]:
    query = urlencode({
        "latitude": lat,
        "longitude": lng,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,uv_index_max",
        "timezone": TIMEZONE_APP,
        "forecast_days": 14,
    })
    return http_get_json(f"https://api.open-meteo.com/v1/forecast?{query}", timeout=timeout, retries=retries)


def _read_json_file(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return json.load(handle)
    except Exception:
        return default


def _write_json_file(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def _cache_age_label(ts: Any) -> str:
    try:
        seconds = max(0, int(time.time() - float(ts or 0)))
    except Exception:
        return "âge inconnu"
    if seconds < 60:
        return "moins d’une minute"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"
    days = hours // 24
    return f"{days} j"


def _meteo_page_cache() -> dict[str, Any]:
    cache = _read_json_file(meteo_page_cache_path(), {})
    return cache if isinstance(cache, dict) else {}


def _save_meteo_page_cache(cache: dict[str, Any]) -> None:
    _write_json_file(meteo_page_cache_path(), cache)


def _city_cache_key(city: dict[str, Any]) -> str:
    return f"{city.get('nom','')}|{float(city.get('lat', 0.0)):.5f}|{float(city.get('lng', 0.0)):.5f}"


def cached_current_weather(city: dict[str, Any], ttl: int = 600, timeout: float = 2.5) -> dict[str, Any]:
    """Météo courante avec cache de secours.

    Le cache court garde le comportement rapide habituel. Si l’API tombe
    après expiration du TTL, on ressort quand même la dernière donnée valide
    au lieu d’afficher ?° partout.
    """
    path = meteo_cache_path()
    cache = _read_json_file(path, {})
    if not isinstance(cache, dict):
        cache = {}
    key = _city_cache_key(city)
    entry = cache.get(key)
    now = time.time()
    if isinstance(entry, dict) and isinstance(entry.get("data"), dict):
        try:
            if now - float(entry.get("ts", 0) or 0) < ttl:
                return entry["data"]
        except Exception:
            pass
    try:
        data = fetch_current_weather(float(city["lat"]), float(city["lng"]), timeout=timeout, retries=1)
        cache[key] = {"ts": now, "data": data}
        _write_json_file(path, cache)
        return data
    except Exception:
        if isinstance(entry, dict) and isinstance(entry.get("data"), dict):
            return entry["data"]
        raise


def cached_forecast(city: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prévisions 14 jours avec cache persistant de secours.

    À chaque chargement de page, on tente l’API. Si elle répond, le cache est
    remplacé. Si elle répond 429/erreur réseau, on affiche la dernière réponse
    valide stockée dans ../conf/meteo-cache.conf.
    """
    cache = _meteo_page_cache()
    forecasts = cache.get("forecasts") if isinstance(cache.get("forecasts"), dict) else {}
    key = _city_cache_key(city)
    entry = forecasts.get(key) if isinstance(forecasts, dict) else None
    now = time.time()
    try:
        data = fetch_forecast(float(city["lat"]), float(city["lng"]))
        if not isinstance(forecasts, dict):
            forecasts = {}
        forecasts[key] = {
            "ts": now,
            "city": str(city.get("nom") or ""),
            "lat": city.get("lat"),
            "lng": city.get("lng"),
            "data": data,
        }
        cache["forecasts"] = forecasts
        cache["updated_at"] = now
        _save_meteo_page_cache(cache)
        return data, {"from_cache": False}
    except Exception as exc:
        if isinstance(entry, dict) and isinstance(entry.get("data"), dict):
            return entry["data"], {
                "from_cache": True,
                "error": str(exc),
                "age": _cache_age_label(entry.get("ts")),
                "message": "Cache : API ne répond pas",
            }
        raise


def _format_temp(value: Any) -> str:
    try:
        number = round(float(value))
        return f"{number}°"
    except Exception:
        return "?°"


def build_top_weather_context() -> dict[str, Any]:
    try:
        top = read_top_conf()
        if top.get("ENABLED") != "1":
            return {"enabled": False}
        city = {
            "nom": top.get("CITY_NAME") or "Météo",
            "lat": float(top.get("LAT") or 0),
            "lng": float(top.get("LNG") or 0),
        }
        if not city["lat"] and not city["lng"]:
            return {"enabled": False}
        cache_path = meteo_top_cache_path()
        cache = _read_json_file(cache_path, {})
        key = _city_cache_key(city)
        now = time.time()
        current = None
        if isinstance(cache, dict) and cache.get("key") == key and now - float(cache.get("ts", 0) or 0) < 600:
            current = cache.get("data") if isinstance(cache.get("data"), dict) else None
        if current is None:
            stale = None
            if isinstance(cache, dict) and cache.get("key") == key and isinstance(cache.get("data"), dict):
                stale = cache.get("data")
            try:
                current = fetch_current_weather(float(city["lat"]), float(city["lng"]), timeout=1.8)
                _write_json_file(cache_path, {"key": key, "ts": now, "data": current})
            except Exception:
                current = stale
        if not isinstance(current, dict):
            return {"enabled": False}
        return {
            "enabled": True,
            "city": city["nom"],
            "icon": get_icon(current.get("weathercode")),
            "temperature": _format_temp(current.get("temperature")),
        }
    except Exception:
        return {"enabled": False}


def _meteo_top_source_mtime() -> float:
    """Dernière modification des fichiers qui pilotent le bandeau météo."""
    mtimes: list[float] = []
    for path in (meteo_top_conf_path(), meteo_top_cache_path()):
        try:
            mtimes.append(os.path.getmtime(path))
        except OSError:
            pass
    return max(mtimes) if mtimes else 0.0


def meteo_top_context_load(force_reload: bool = False) -> dict[str, Any]:
    """Charge ou relit le bandeau météo en mémoire.

    Le cache mémoire évite un appel API à chaque page. Si un cron externe met à
    jour les fichiers de cache, `meteo_top_context_cached()` détecte le mtime et
    recharge ce contexte au prochain rendu, sans boucle permanente.
    """
    global METEO_TOP_CONTEXT_CACHE, METEO_TOP_CONTEXT_CACHE_LOADED_AT, METEO_TOP_CONTEXT_SOURCE_MTIME
    with METEO_TOP_CONTEXT_LOCK:
        if not force_reload and isinstance(METEO_TOP_CONTEXT_CACHE, dict):
            return copy.deepcopy(METEO_TOP_CONTEXT_CACHE)

    context = build_top_weather_context()
    if not isinstance(context, dict):
        context = {"enabled": False}

    with METEO_TOP_CONTEXT_LOCK:
        METEO_TOP_CONTEXT_CACHE = copy.deepcopy(context)
        METEO_TOP_CONTEXT_CACHE_LOADED_AT = time.time()
        METEO_TOP_CONTEXT_SOURCE_MTIME = _meteo_top_source_mtime()
        return copy.deepcopy(METEO_TOP_CONTEXT_CACHE)


def meteo_top_context_cached() -> dict[str, Any]:
    """Retourne le contexte du bandeau et le recharge si le cache disque a changé."""
    global METEO_TOP_CONTEXT_CACHE_LOADED_AT, METEO_TOP_CONTEXT_SOURCE_MTIME
    now = time.time()
    with METEO_TOP_CONTEXT_LOCK:
        cached = copy.deepcopy(METEO_TOP_CONTEXT_CACHE) if isinstance(METEO_TOP_CONTEXT_CACHE, dict) else None
        loaded_at = float(METEO_TOP_CONTEXT_CACHE_LOADED_AT or 0.0)
        known_mtime = float(METEO_TOP_CONTEXT_SOURCE_MTIME or 0.0)

    if cached is not None and now - loaded_at < METEO_TOP_CONTEXT_DISK_CHECK_TTL:
        return cached

    current_mtime = _meteo_top_source_mtime()
    if cached is not None and current_mtime <= known_mtime:
        with METEO_TOP_CONTEXT_LOCK:
            METEO_TOP_CONTEXT_CACHE_LOADED_AT = now
        return cached

    # Un cron ou une action UI a modifié meteo-top-cache.json/meteo-top.conf :
    # on recharge. Si le cache disque est frais, build_top_weather_context() ne
    # refait pas d'appel réseau.
    return meteo_top_context_load(force_reload=True)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@meteo_bp.app_context_processor
def inject_meteo_top() -> dict[str, Any]:
    return {"meteo_top": meteo_top_context_cached()}



@meteo_bp.route("/system/personnalisation/meteo", methods=["GET", "POST"])
def meteo_home():
    if request.method == "POST":
        return meteo_action()
    ensure_meteo_conf()
    cities = load_cities()
    try:
        active_id = int(request.args.get("id", "0"))
    except Exception:
        active_id = 0
    if active_id < 0 or active_id >= len(cities):
        active_id = 0
    top_conf = read_top_conf()
    top_enabled = top_conf.get("ENABLED") == "1"
    top_city_name = top_conf.get("CITY_NAME", "") if top_enabled else ""

    city_cards: list[dict[str, Any]] = []
    for index, city in enumerate(cities):
        try:
            current = cached_current_weather(city)
            temp = _format_temp(current.get("temperature"))
            icon = get_icon(current.get("weathercode"))
        except Exception:
            temp = "?°"
            icon = "❓"
        city_cards.append({
            "id": index,
            "nom": city["nom"],
            "lat": city["lat"],
            "lng": city["lng"],
            "temp": temp,
            "icon": icon,
            "active": index == active_id,
            "top": top_enabled and str(city["nom"]) == top_city_name,
        })

    return render_template(
        "meteo.html",
        page_icon_override="🌦️",
        cities=city_cards,
        active_id=active_id,
        meteo_conf_path=meteo_conf_path(),
        meteo_jdom_path=meteo_jdom_path(),
        meteo_top_conf_path=meteo_top_conf_path(),
        top_enabled=top_enabled,
        top_city_name=top_city_name,
    )



@meteo_bp.route("/system/personnalisation/meteo/action", methods=["POST"])
@meteo_bp.route("/system/personnalisation/meteo/save", methods=["POST"])
@meteo_bp.route("/system/personnalisation/meteo/top/save", methods=["POST"])
@meteo_bp.route("/system/personnalisation/meteo/api/top", methods=["POST"])
def meteo_action():
    ensure_meteo_conf()
    cities = load_cities()
    action = str(request.form.get("action") or "").strip()

    if action == "ajouter":
        nom = str(request.form.get("nom") or "").strip()
        try:
            lat = float(request.form.get("lat") or "")
            lng = float(request.form.get("lng") or "")
        except Exception:
            lat = lng = None  # type: ignore
        if nom and lat is not None and lng is not None:
            exists = any(str(city.get("nom", "")).lower() == nom.lower() for city in cities)
            if not exists:
                cities.append({"nom": nom, "lat": lat, "lng": lng})
                save_cities(cities)
                return redirect(url_for("meteo_bp.meteo_home", id=len(cities) - 1))
        return redirect(url_for("meteo_bp.meteo_home"))

    if action == "supprimer":
        selected = set(str(item) for item in request.form.getlist("villes_check"))
        if selected and len(selected) < len(cities):
            cities = [city for city in cities if str(city.get("nom")) not in selected]
            save_cities(cities)
            top = read_top_conf()
            if top.get("CITY_NAME") in selected:
                write_top_conf(None)
                meteo_top_context_load(force_reload=True)
        return redirect(url_for("meteo_bp.meteo_home"))

    if action == "reordonner":
        order = [str(item) for item in request.form.getlist("ordre_villes")]
        by_name = {str(city.get("nom")): city for city in cities}
        reordered = [by_name[name] for name in order if name in by_name]
        if reordered:
            save_cities(reordered)
        return redirect(url_for("meteo_bp.meteo_home"))

    if action == "top_apply":
        wants_json = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in (request.headers.get("Accept") or "")
        )
        raw_idx = str(request.form.get("top_city") or "").strip()

        try:
            if raw_idx == "":
                write_top_conf(None)
                meteo_top_context_load(force_reload=True)
                if wants_json:
                    return jsonify({
                        "ok": True,
                        "message": "Enregistré",
                        "meteo_top": {"enabled": False},
                    })
                return redirect(url_for("meteo_bp.meteo_home", id=request.form.get("active_id", 0)))

            try:
                idx = int(raw_idx)
                city = cities[idx]
            except Exception:
                idx = 0
                city = None

            write_top_conf(city)
            meteo_top_context_load(force_reload=True)
            if wants_json:
                if city:
                    top_payload = {
                        "enabled": True,
                        "city": str(city.get("nom") or ""),
                        "icon": str(request.form.get("top_icon") or "🌤️"),
                        "temperature": str(request.form.get("top_temperature") or ""),
                    }
                else:
                    top_payload = {"enabled": False}
                return jsonify({
                    "ok": True,
                    "message": "Enregistré",
                    "meteo_top": top_payload,
                    "active_id": idx if city else 0,
                })
            return redirect(url_for("meteo_bp.meteo_home", id=raw_idx if city else 0))
        except Exception as exc:
            if wants_json:
                return jsonify({
                    "ok": False,
                    "message": f"Enregistrement impossible : {exc}",
                }), 500
            raise

    return redirect(url_for("meteo_bp.meteo_home"))



@meteo_bp.route("/system/personnalisation/meteo/api/recherche", methods=["GET"])
def meteo_search():
    query = str(request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify([])
    params = urlencode({"name": query, "count": 8, "language": "fr", "format": "json"})
    try:
        data = http_get_json(f"https://geocoding-api.open-meteo.com/v1/search?{params}", timeout=5.0)
        results = data.get("results") if isinstance(data, dict) else []
        return jsonify(results if isinstance(results, list) else [])
    except Exception:
        return jsonify([])


# ---------------------------------------------------------------------------
# CLI / actualisation planifiable
# ---------------------------------------------------------------------------
def refresh_meteo_cache(include_forecasts: bool = True, top_only: bool = False) -> dict[str, Any]:
    """Actualise les caches météo depuis un cron/tâche externe.

    Cette fonction est volontairement sans boucle : elle fait un passage, écrit
    les caches disque, puis quitte. Le gestionnaire de tâches choisit ensuite la
    fréquence Linux/cron.
    """
    ensure_meteo_conf()
    cities = load_cities()
    top = read_top_conf()
    now = time.time()
    result: dict[str, Any] = {
        "ok": True,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "current_ok": 0,
        "forecast_ok": 0,
        "top_ok": False,
        "top_cache_used": False,
        "warnings": [],
        "errors": [],
    }

    current_cache = _read_json_file(meteo_cache_path(), {})
    if not isinstance(current_cache, dict):
        current_cache = {}

    page_cache = _meteo_page_cache()
    forecasts = page_cache.get("forecasts") if isinstance(page_cache.get("forecasts"), dict) else {}
    if not isinstance(forecasts, dict):
        forecasts = {}

    # Ville du bandeau : on la traite toujours, même si top_only=1.
    top_city: dict[str, Any] | None = None
    if top.get("ENABLED") == "1":
        try:
            top_city = {
                "nom": top.get("CITY_NAME") or "Météo",
                "lat": float(top.get("LAT") or 0),
                "lng": float(top.get("LNG") or 0),
            }
        except Exception as exc:
            result["errors"].append(f"Bandeau: coordonnées invalides: {exc}")

    if top_city and (top_city.get("lat") or top_city.get("lng")):
        key = _city_cache_key(top_city)
        top_cache = _read_json_file(meteo_top_cache_path(), {})
        stale_top = None
        stale_ts = now
        if isinstance(top_cache, dict) and top_cache.get("key") == key and isinstance(top_cache.get("data"), dict):
            stale_top = top_cache.get("data")
            try:
                stale_ts = float(top_cache.get("ts") or now)
            except Exception:
                stale_ts = now
        try:
            current = fetch_current_weather(float(top_city["lat"]), float(top_city["lng"]), timeout=6.0, retries=2)
            current_cache[key] = {"ts": now, "data": current}
            _write_json_file(meteo_top_cache_path(), {"key": key, "ts": now, "data": current})
            result["top_ok"] = True
        except Exception as exc:
            if isinstance(stale_top, dict):
                current_cache[key] = {"ts": stale_ts, "data": stale_top}
                result["top_ok"] = True
                result["top_cache_used"] = True
                result["warnings"].append(f"Bandeau {top_city.get('nom')}: API indisponible, cache conservé ({exc})")
            else:
                result["ok"] = False
                result["errors"].append(f"Bandeau {top_city.get('nom')}: {exc}")

    if not top_only:
        for city in cities:
            name = str(city.get("nom") or "?")
            try:
                current = fetch_current_weather(float(city["lat"]), float(city["lng"]), timeout=5.0, retries=1)
                current_cache[_city_cache_key(city)] = {"ts": now, "data": current}
                result["current_ok"] += 1
            except Exception as exc:
                result["ok"] = False
                result["errors"].append(f"Actuel {name}: {exc}")

            if include_forecasts:
                try:
                    forecast = fetch_forecast(float(city["lat"]), float(city["lng"]), retries=1)
                    forecasts[_city_cache_key(city)] = {
                        "ts": now,
                        "city": name,
                        "lat": city.get("lat"),
                        "lng": city.get("lng"),
                        "data": forecast,
                    }
                    result["forecast_ok"] += 1
                except Exception as exc:
                    result["ok"] = False
                    result["errors"].append(f"Prévisions {name}: {exc}")

    _write_json_file(meteo_cache_path(), current_cache)
    if include_forecasts and not top_only:
        page_cache["forecasts"] = forecasts
        page_cache["updated_at"] = now
        _save_meteo_page_cache(page_cache)

    # Utile si la commande est lancée dans le même process, et sans effet gênant
    # quand elle est lancée par cron dans un process séparé.
    try:
        meteo_top_context_load(force_reload=True)
    except Exception:
        pass

    return result


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Météo Yoleo")
    parser.add_argument("--update", "--refresh", action="store_true", help="Actualise les caches météo puis quitte")
    parser.add_argument("--top-only", action="store_true", help="Actualise seulement la météo du bandeau haut")
    parser.add_argument("--current-only", action="store_true", help="N'actualise pas les prévisions 14 jours")
    parser.add_argument("--json", action="store_true", help="Affiche le résultat en JSON")
    args = parser.parse_args(argv)

    if not args.update:
        parser.print_help()
        return 1

    result = refresh_meteo_cache(include_forecasts=not args.current_only, top_only=args.top_only)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Actualisation météo terminée")
        if result.get("top_ok"):
            print(f"- Bandeau : {'OK (cache conservé)' if result.get('top_cache_used') else 'OK'}")
        else:
            print("- Bandeau : désactivé/erreur")
        print(f"- Météo actuelle : {result.get('current_ok', 0)} ville(s)")
        if not args.current_only and not args.top_only:
            print(f"- Prévisions : {result.get('forecast_ok', 0)} ville(s)")
        for warning in result.get("warnings") or []:
            print(f"⚠️ {warning}")
        for error in result.get("errors") or []:
            print(f"⚠️ {error}")
    return 0 if result.get("ok") else 2


if __name__ != "__main__":
    try:
        meteo_top_context_load(force_reload=True)
        print("Cache meteo bandeau charge en memoire.")
    except Exception as exc:
        print(f"Cache meteo bandeau non charge : {exc}")


if __name__ == "__main__":
    sys.exit(cli_main())
