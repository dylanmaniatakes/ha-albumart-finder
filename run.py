#!/usr/bin/env python3
"""
Home Assistant Album Art Finder

- Subscribes to an MQTT topic (default: media/gym) that publishes JSON like:
  {
    "state": "playing",
    "title": "Absence Of You",
    "artist": "PALESKIN",
    "album": "coreradio"
  }
- Looks up album/track artwork via the public iTunes Search API (no auth).
- Hosts the current artwork at /albumart.jpg and a JSON status at /status.

Configuration sources (in order):
  1) Environment variables already set in the shell/process
  2) A local .env file (default: ./.env) — parsed by this script

You can change the .env path by setting ENV_FILE to a different path.

Key envs (also supported in .env):
  MQTT_HOST        (required)
  MQTT_PORT        (default 1883)
  MQTT_USERNAME    (optional)
  MQTT_PASSWORD    (optional)
  MQTT_TOPIC       (default "media/gym")
  HTTP_HOST        (default "0.0.0.0")
  HTTP_PORT        (default 8099)
  STATIC_DIR       (default "static")

Run locally:
  pip install flask paho-mqtt requests
  # create .env (see template printed in README section)
  python run.py

As a HA add-on, you can source values from options and write a .env before launching.
"""
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import requests
from flask import Flask, jsonify, make_response, send_file
import paho.mqtt.client as mqtt

# ----------------------
# Minimal .env loader (no external dependency)
# ----------------------

def load_env_file(path: str = ".env", override: bool = False) -> int:
    """Load KEY=VALUE lines from a .env file into os.environ.

    - Lines beginning with '#' or blank lines are ignored.
    - Values may be quoted with single or double quotes; surrounding quotes are stripped.
    - If override=False, existing os.environ keys are not replaced.
    Returns the number of variables loaded.
    """
    loaded = 0
    p = Path(path)
    if not p.exists():
        logging.getLogger("albumart-finder").debug(".env not found at %s", p)
        return 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        # Strip surrounding quotes
        if (val.startswith("\"") and val.endswith("\"")) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        if not override and key in os.environ:
            continue
        os.environ[key] = val
        loaded += 1
    return loaded


# ----------------------
# Configuration
# ----------------------
# Load from .env before reading config values
ENV_FILE = os.getenv("ENV_FILE", ".env")
loaded_count = load_env_file(ENV_FILE, override=False)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("albumart-finder")
if loaded_count:
    logger.info("Loaded %d variable(s) from %s", loaded_count, ENV_FILE)

MQTT_HOST = os.getenv("MQTT_HOST")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "media/gym")

HTTP_HOST = os.getenv("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("HTTP_PORT", "8099"))
STATIC_DIR = Path(os.getenv("STATIC_DIR", "static"))
STATIC_DIR.mkdir(parents=True, exist_ok=True)
ALBUMART_PATH = STATIC_DIR / "albumart.jpg"

# ----------------------
# Global state
# ----------------------
_latest_meta: Dict[str, Optional[str]] = {}
_last_update_ts: float = 0.0

# A tiny 1x1 JPEG placeholder (white pixel) so the endpoint always exists.
_PLACEHOLDER_JPEG = (
    b"\xff\xd8\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c !,'\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xd2\xff\xd9"
)

if not ALBUMART_PATH.exists():
    ALBUMART_PATH.write_bytes(_PLACEHOLDER_JPEG)


# ----------------------
# Artwork lookup
# ----------------------

def _search_itunes(query: str, entity: str = "song") -> Optional[str]:
    """Return artwork URL (prefer hi-res) for a query via iTunes Search API."""
    try:
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={"term": query, "entity": entity, "limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return None
        art = results[0].get("artworkUrl100")
        if not art:
            return None
        # Upgrade size: .../100x100bb.jpg -> /600x600bb.jpg
        return art.replace("100x100", "600x600")
    except Exception as e:
        logger.warning("iTunes lookup failed for '%s': %s", query, e)
        return None


def find_album_art(artist: Optional[str], title: Optional[str], album: Optional[str]) -> Optional[bytes]:
    """Try to fetch album art image bytes using several queries."""
    queries = []
    if artist and title:
        queries.append((f"{artist} {title}", "song"))
    if artist and album:
        queries.append((f"{artist} {album}", "album"))
    if title:
        queries.append((title, "song"))
    if album:
        queries.append((album, "album"))

    for q, entity in queries:
        url = _search_itunes(q, entity=entity)
        if not url:
            continue
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            if r.content and r.headers.get("Content-Type", "").startswith("image/"):
                return r.content
        except Exception as e:
            logger.warning("Failed to download art from %s: %s", url, e)
    return None


def update_art_from_meta(meta: Dict[str, Optional[str]]) -> bool:
    """Given a metadata dict, fetch and store album art. Returns True on change."""
    global _latest_meta, _last_update_ts
    # Only fetch if actually playing and we have some identifier
    state = (meta.get("state") or "").lower()
    title = meta.get("title")
    artist = meta.get("artist")
    album = meta.get("album")

    if state not in {"playing", "paused"}:
        logger.info("State is '%s' (not playing/paused); keeping existing art.", state)
        return False

    art_bytes = find_album_art(artist, title, album)
    if not art_bytes:
        logger.info("No art found for %s - %s (%s)", artist, title, album)
        return False

    # Write atomically to avoid partial reads by HTTP clients
    tmp_path = ALBUMART_PATH.with_suffix(".jpg.tmp")
    tmp_path.write_bytes(art_bytes)
    tmp_path.replace(ALBUMART_PATH)

    _latest_meta = {
        "state": state,
        "title": title,
        "artist": artist,
        "album": album,
        "updated": time.time(),
    }
    _last_update_ts = time.time()
    logger.info("Updated album art: %s - %s (%s)", artist, title, album)
    return True


# ----------------------
# MQTT client
# ----------------------

def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        logger.info("Connected to MQTT at %s:%s", MQTT_HOST, MQTT_PORT)
        client.subscribe(MQTT_TOPIC, qos=0)
        logger.info("Subscribed to topic: %s", MQTT_TOPIC)
    else:
        logger.error("MQTT connect failed: %s", reason_code)


def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode("utf-8", errors="replace")
        meta = json.loads(payload)
        if not isinstance(meta, dict):
            logger.warning("MQTT payload is not an object: %s", payload[:200])
            return
        changed = update_art_from_meta(meta)
        if changed:
            logger.debug("Art updated from topic %s", msg.topic)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON on %s: %s", msg.topic, msg.payload[:200])
    except Exception as e:
        logger.exception("Error processing MQTT message: %s", e)


def start_mqtt_loop():
    if not MQTT_HOST:
        logger.error("MQTT_HOST is required. Set it via environment variable or .env file.")
        return
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="albumart-finder")
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_forever()


# ----------------------
# HTTP server
# ----------------------
app = Flask(__name__)


@app.get("/albumart.jpg")
def serve_album_art():
    # Cache-bust friendly: allow caches but require revalidation
    resp = make_response(send_file(ALBUMART_PATH, mimetype="image/jpeg"))
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    resp.headers["ETag"] = str(int(_last_update_ts))
    return resp


@app.get("/")
@app.get("/status")
def status():
    info = {
        "topic": MQTT_TOPIC,
        "last_update_epoch": _last_update_ts,
        "meta": _latest_meta,
        "albumart_path": str(ALBUMART_PATH.resolve()),
    }
    return jsonify(info)


def start_http():
    logger.info("HTTP server listening on %s:%s", HTTP_HOST, HTTP_PORT)
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)


# ----------------------
# Main
# ----------------------
if __name__ == "__main__":
    # Start threads for MQTT and HTTP
    http_t = threading.Thread(target=start_http, daemon=True)
    http_t.start()

    start_mqtt_loop()
