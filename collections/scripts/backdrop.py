#!/usr/bin/env python3
"""
Backdrop generator driven entirely by explicit TMDB request parameters.

Purpose:
    This script generates one backdrop image set from explicit TMDB request
    specs. It does not inspect `Nuvio-Collections.json` itself. Use it when
    you already know which TMDB requests should be merged into one backdrop.
    When Fanart artwork is enabled, language selection is prioritized in this
    order: preferred language, original title language, TMDB backdrop fallback,
    textless/no-language artwork, and only then any other non-empty Fanart
    language if everything else fails.

Important parameters:
    --api-key
        TMDB API key used for metadata discovery requests.
    --fanart-key
        Optional Fanart.tv API key. If provided, the script prefers Fanart
        thumbs/logos and falls back to TMDB backdrops.
    --preferred-language
        Preferred Fanart artwork language code. Defaults to `en`.
    --label
        Human-readable label used in logs and for fallback accent generation.
    --tmdb-request
        One TMDB request spec. Repeat this flag to merge multiple catalogs into
        the same backdrop.
    --accent-color
        Optional accent color for the gradient overlay. Accepts `#RRGGBB` or
        `R,G,B`.
    --output
        Exact output file path to write.
    --output-dir
        Output directory used only when `--output` is not provided.
    --size
        Output size: `4k`, `1080p`, or `both`.
    --profile
        Named output profile: `compressed` or `high`.
    --quality
        Advanced manual output quality override used for both JPG and WEBP.
    --focus
        Grid focus preset or explicit `x,y` fractions.
    --count
        Maximum number of titles to keep after all request sets are merged.

Request format:
    movie:key=value&key=value
    tv:key=value&key=value
    movie:/movie/popular?language=en-US
    tv:/trending/tv/week?language=en-US

Examples:
    python3 -B backdrop.py \
      --api-key YOUR_TMDB_KEY \
      --fanart-key YOUR_FANART_KEY \
      --label "Netflix" \
      --accent-color "213,30,39" \
      --tmdb-request 'movie:sort_by=popularity.desc&with_watch_providers=8&watch_region=US' \
      --tmdb-request 'tv:sort_by=popularity.desc&with_watch_providers=8&watch_region=US' \
      --output /tmp/netflix.jpg \
      --size 4k
"""

import argparse
import colorsys
import contextlib
import io
import itertools
import math
import os
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output"
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p"
BACKDROP_SIZE = "w1280"
FANART_BASE = "https://webservice.fanart.tv/v3"
MDBLIST_BASE = "https://api.mdblist.com"
QUALITY_PRESETS = {
    "compressed": {"quality": 82, "progressive": True, "subsampling": "4:2:0"},
    "high": {"quality": 95, "progressive": False, "subsampling": 0},
}

# ---------------------------------------------------------------------------
# Collage layout (tilted-overlap)
# ---------------------------------------------------------------------------
# Landscape (16:9) tiles arranged in overlapping rows, with the whole grid
# rotated by TILT_DEG. Tweak these values (or pass the matching --tile-width /
# --tile-height / --tilt-deg / --gap / --stagger / --card-radius flags) to
# restyle the collage.
CARD_RADIUS = 9        # corner radius of each tile (px)
TILT_DEG = 10          # rotation applied to the whole grid (degrees)
TILE_W = 372           # tile width at scale 1.0 (px)
TILE_H = 210           # tile height at scale 1.0 (px, 16:9)
GAP = 9                # gap between tiles (px)
ROWS = 10              # logical rows in the source grid
COLS = 10              # logical columns in the source grid
STAGGER = 0.5          # horizontal row offset (fraction of tile step)
FOCUS_X = 0.5          # horizontal focal point (fraction of grid width)
FOCUS_Y = 0.53         # vertical focal point (fraction of grid height)

FOCUS_PRESETS = {
    "center": (0.50, 0.50),
    "top-right": (0.72, 0.28),
    "center-right": (0.65, 0.45),
    "top-center": (0.52, 0.30),
}

SIZE_PRESETS = {
    "4k": (3840, 2160, 3840 / 1920),
    "1080p": (1920, 1080, 1.0),
}


def cleanup_pycache():
    """Remove the local __pycache__ folder if one was created."""
    shutil.rmtree(SCRIPT_DIR / "__pycache__", ignore_errors=True)


def normalize_media_type(value):
    if value == "series":
        return "tv"
    if value in {"movie", "tv"}:
        return value
    raise ValueError(f"Unsupported media type '{value}'.")


def default_accent_for_label(label):
    seed = sum((index + 1) * ord(char) for index, char in enumerate(label or "Backdrop"))
    hue = (seed % 360) / 360.0
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.65, 0.88)
    return (int(red * 255), int(green * 255), int(blue * 255))


def parse_accent_color(value):
    if not value:
        return None
    value = value.strip()
    if value.startswith("#"):
        value = value[1:]
        if len(value) != 6:
            raise ValueError("Hex accent colors must use 6 digits, like #2299aa.")
        return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))

    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 3:
        raise ValueError("Accent colors must be '#RRGGBB' or 'R,G,B'.")
    rgb = tuple(int(part) for part in parts)
    if any(part < 0 or part > 255 for part in rgb):
        raise ValueError("Accent color channels must be between 0 and 255.")
    return rgb


def tmdb_get(endpoint, params, api_key):
    query = dict(params)
    query["api_key"] = api_key
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(f"{TMDB_BASE}{endpoint}", params=query, timeout=20)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            last_error = exc
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code is None or status_code < 500 or attempt == 2:
                raise
            time.sleep(1 + attempt)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
    raise last_error


def parse_request_spec(spec):
    """Parse a request spec into a TMDB discover/endpoint call or an MDBList list."""
    try:
        raw_kind, raw_request = spec.split(":", 1)
    except ValueError as exc:
        raise ValueError(
            f"Invalid request '{spec}'. Use 'movie:key=value&...', 'tv:/path?query=...' or 'mdblist:<id>:<type>'."
        ) from exc

    raw_request = raw_request.strip()
    if raw_kind.strip() == "mdblist":
        list_id, _, media_type = raw_request.partition(":")
        list_id = list_id.strip()
        if not list_id:
            raise ValueError(f"Invalid mdblist request '{spec}': missing list id.")
        return {
            "mode": "mdblist",
            "list_id": list_id,
            "media_type": normalize_media_type(media_type.strip() or "movie"),
        }

    media_type = normalize_media_type(raw_kind.strip())
    if not raw_request:
        raise ValueError(f"Invalid request '{spec}': missing path or query string.")

    if raw_request.startswith("/"):
        path, _, query_string = raw_request.partition("?")
        params = dict(parse_qsl(query_string, keep_blank_values=True))
        if path.startswith("/discover/"):
            return {"mode": "discover", "media_type": media_type, "params": params}
        return {"mode": "endpoint", "media_type": media_type, "path": path, "params": params}

    params = dict(parse_qsl(raw_request, keep_blank_values=True))
    return {"mode": "discover", "media_type": media_type, "params": params}


def fetch_mdblist_titles(spec, tmdb_api_key, mdblist_key):
    """Fetch titles in an MDBList list and resolve them to TMDB items."""
    if not mdblist_key:
        return []
    items = []
    try:
        response = requests.get(
            f"{MDBLIST_BASE}/lists/{spec['list_id']}/items",
            params={"apikey": mdblist_key},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        return items

    if isinstance(data, dict):
        if "movies" in data or "shows" in data:
            data = (data.get("movies") or []) + (data.get("shows") or [])
        else:
            data = data.get("items") or data.get("results") or data.get("data") or []

    for entry in data or []:
        ids = entry.get("ids") or {}
        tmdb_id = ids.get("tmdb") or entry.get("id")
        if not tmdb_id:
            continue
        mediatype = str(entry.get("mediatype") or entry.get("type") or "").lower()
        kind = "tv" if mediatype in ("tv", "show", "series", "tv_show") else "movie"
        try:
            detail = tmdb_get(f"/{kind}/{tmdb_id}", {}, tmdb_api_key)
        except Exception:
            continue
        if detail.get("backdrop_path"):
            items.append((kind, detail))
    return items


def fetch_titles_for_spec(spec, api_key, max_pages=3, mdblist_key=None):
    if spec["mode"] == "mdblist":
        return fetch_mdblist_titles(spec, api_key, mdblist_key)
    items = []
    if spec["mode"] == "discover":
        endpoint = f"/discover/{spec['media_type']}"
        base_params = dict(spec["params"])
    else:
        endpoint = spec["path"]
        base_params = dict(spec.get("params", {}))

    for page in range(1, max_pages + 1):
        data = tmdb_get(endpoint, {**base_params, "page": page}, api_key)
        page_results = data.get("results", [])
        if not page_results:
            break
        for item in page_results:
            if item.get("backdrop_path"):
                items.append((spec["media_type"], item))
        total_pages = data.get("total_pages") or max_pages
        if page >= total_pages:
            break
    return items


def fetch_titles(request_specs, api_key, count=60, mdblist_key=None):
    # Interleave results from each request so mixed folders (for example movie + TV)
    # do not get visually dominated by the first request in the list.
    per_spec_items = [fetch_titles_for_spec(spec, api_key, mdblist_key=mdblist_key) for spec in request_specs]
    merged = []
    max_len = max((len(spec_items) for spec_items in per_spec_items), default=0)
    for index in range(max_len):
        for spec_items in per_spec_items:
            if index < len(spec_items):
                merged.append(spec_items[index])

    seen = set()
    unique = []
    for media_type, item in merged:
        key = (media_type, item["id"])
        if key in seen:
            continue
        seen.add(key)
        unique.append((media_type, item))
        if len(unique) >= count:
            break
    return unique


def get_tmdb_external_ids(kind, tmdb_id, api_key):
    try:
        return tmdb_get(f"/{kind}/{tmdb_id}/external_ids", {}, api_key)
    except Exception:
        return {}


def fanart_get_tv(tvdb_id, fanart_key):
    try:
        response = requests.get(f"{FANART_BASE}/tv/{tvdb_id}", params={"api_key": fanart_key}, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def fanart_get_movie(tmdb_id, fanart_key):
    try:
        response = requests.get(f"{FANART_BASE}/movies/{tmdb_id}", params={"api_key": fanart_key}, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def fanart_candidate_groups(fanart_data, kind):
    """Return preferred thumb/background candidate groups in priority order."""
    if not fanart_data:
        return []
    if kind == "tv":
        return [
            ("thumb", fanart_data.get("tvthumb") or []),
            ("background", fanart_data.get("showbackground") or []),
        ]
    else:
        return [
            ("thumb", fanart_data.get("moviethumb") or []),
            ("background", fanart_data.get("moviebackground") or []),
        ]


def normalize_fanart_lang(value):
    if value is None:
        return None
    value = str(value).strip().lower()
    if value in {"", "00", "none", "null"}:
        return None
    return value


def pick_fanart_url(fanart_data, kind, preferred_language, original_language):
    """
    Fanart selection priority:
    1. preferred language
    2. original title language
    3. textless / no language
    4. any other non-empty language
    """
    preferred_language = normalize_fanart_lang(preferred_language)
    original_language = normalize_fanart_lang(original_language)

    ranked_groups = {
        "preferred": [],
        "original": [],
        "textless": [],
        "other": [],
    }

    for group_rank, (_, candidates) in enumerate(fanart_candidate_groups(fanart_data, kind)):
        if not candidates:
            continue

        for candidate in candidates:
            lang = normalize_fanart_lang(candidate.get("lang"))
            entry = {"candidate": candidate, "group_rank": group_rank}
            if preferred_language and lang == preferred_language:
                ranked_groups["preferred"].append(entry)
            elif original_language and lang == original_language:
                ranked_groups["original"].append(entry)
            elif lang is None:
                ranked_groups["textless"].append(entry)
            elif lang:
                ranked_groups["other"].append(entry)

    for bucket in ("preferred", "original", "textless", "other"):
        if ranked_groups[bucket]:
            best = sorted(
                ranked_groups[bucket],
                key=lambda entry: (entry["group_rank"], -int(entry["candidate"].get("likes", 0))),
            )[0]["candidate"]
            if best.get("url"):
                return best["url"], bucket

    return None, None


def download_image_url(url, retries=2):
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            return Image.open(io.BytesIO(response.content)).convert("RGBA")
        except Exception as exc:
            if attempt == retries:
                print(f"  ! Failed to download {url}: {exc}")
                return None
            time.sleep(1)


def download_tmdb_backdrop(path, retries=2):
    return download_image_url(f"{TMDB_IMG_BASE}/{BACKDROP_SIZE}{path}", retries=retries)


def fetch_tile_image(kind, item, api_key, fanart_key, preferred_language):
    tmdb_id = item["id"]
    original_language = item.get("original_language")
    preferred_url = None
    textless_url = None
    last_resort_url = None

    if fanart_key:
        if kind == "tv":
            external_ids = get_tmdb_external_ids("tv", tmdb_id, api_key)
            tvdb_id = external_ids.get("tvdb_id")
            if tvdb_id:
                candidate_url, bucket = pick_fanart_url(
                    fanart_get_tv(tvdb_id, fanart_key),
                    "tv",
                    preferred_language,
                    original_language,
                )
                if bucket == "other":
                    last_resort_url = candidate_url
                elif bucket == "textless":
                    textless_url = candidate_url
                else:
                    preferred_url = candidate_url
        else:
            candidate_url, bucket = pick_fanart_url(
                fanart_get_movie(tmdb_id, fanart_key),
                "movie",
                preferred_language,
                original_language,
            )
            if bucket == "other":
                last_resort_url = candidate_url
            elif bucket == "textless":
                textless_url = candidate_url
            else:
                preferred_url = candidate_url

    if preferred_url:
        image = download_image_url(preferred_url)
        if image:
            return image, "fanart"

    tmdb_image = download_tmdb_backdrop(item["backdrop_path"])
    if tmdb_image:
        return tmdb_image, "tmdb"

    if textless_url:
        image = download_image_url(textless_url)
        if image:
            return image, "fanart"

    if last_resort_url:
        image = download_image_url(last_resort_url)
        if image:
            return image, "fanart_other_language"

    return None, "missing"


def fetch_tmdb_logo(media_type, tmdb_id, api_key):
    """Fetch the title's clear logo (transparent PNG) from TMDB, if available."""
    try:
        data = tmdb_get(f"/{media_type}/{tmdb_id}/images", {}, api_key)
    except Exception:
        return None
    logos = data.get("logos") or []
    if not logos:
        return None
    english = [logo for logo in logos if (logo.get("iso_639_1") or "").lower() == "en"]
    pool = english or logos
    # PIL cannot decode SVG logos; skip them so we fall back to a text title.
    pool = [logo for logo in pool if not (logo.get("file_path") or "").lower().endswith(".svg")]
    if not pool:
        return None
    best = sorted(
        pool,
        key=lambda logo: (-(logo.get("vote_count") or 0), -(logo.get("vote_average") or 0)),
    )[0]
    file_path = best.get("file_path")
    if not file_path:
        return None
    return download_image_url(f"{TMDB_IMG_BASE}/w500{file_path}")


def rounded_rect_mask(width, height, radius=CARD_RADIUS):
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=radius, fill=255)
    return mask


def make_tile(image, tile_width, tile_height):
    source_width, source_height = image.size
    target_ratio = tile_width / tile_height
    current_ratio = source_width / source_height
    if current_ratio > target_ratio:
        new_width = int(source_height * target_ratio)
        left = (source_width - new_width) // 2
        image = image.crop((left, 0, left + new_width, source_height))
    else:
        new_height = int(source_width / target_ratio)
        top = (source_height - new_height) // 2
        image = image.crop((0, top, source_width, top + new_height))
    image = image.resize((tile_width, tile_height), Image.LANCZOS)
    scaled_radius = max(8, int(CARD_RADIUS * tile_width / TILE_W))
    mask = rounded_rect_mask(tile_width, tile_height, radius=scaled_radius)
    result = Image.new("RGBA", (tile_width, tile_height), (0, 0, 0, 0))
    result.paste(image, mask=mask)
    return result


FONT_CANDIDATES = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)


def load_title_font(size):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def draw_title_overlay(tile, title):
    """Draw the movie/show title at the bottom of a tile with a dark backing."""
    title = (title or "").strip()
    if not title:
        return tile
    width, height = tile.size

    size = max(16, width // 10)
    font = load_title_font(size)
    max_text_width = int(width * 0.94)
    draw = ImageDraw.Draw(tile)

    while size > 16:
        bbox = draw.textbbox((0, 0), title, font=font)
        if bbox[2] - bbox[0] <= max_text_width:
            break
        size -= 2
        font = load_title_font(size)

    while True:
        bbox = draw.textbbox((0, 0), title, font=font)
        if bbox[2] - bbox[0] <= max_text_width or len(title) <= 4:
            break
        title = title[:-2] + "…"

    bbox = draw.textbbox((0, 0), title, font=font)
    text_height = bbox[3] - bbox[1]
    pad_x = max(6, int(width * 0.03))
    pad_y = max(4, int(text_height * 0.35))
    x = pad_x
    y = height - text_height - pad_y
    strip_height = text_height + pad_y * 2

    alpha = tile.getchannel("A")
    strip = Image.new("RGBA", (width, strip_height), (0, 0, 0, 120))
    mask = alpha.crop((0, height - strip_height, width, height))
    tile.paste(strip, (0, height - strip_height), mask)

    draw.text((x, y), title, font=font, fill=(255, 255, 255, 255))
    return tile


def overlay_clear_logo(tile, logo):
    """Center the title's clear logo over a tile, scaled to fit."""
    if logo is None:
        return tile
    logo = logo.copy()
    tile_width, tile_height = tile.size
    max_width = int(tile_width * 0.80)
    max_height = int(tile_height * 0.55)
    scale = min(max_width / logo.width, max_height / logo.height)
    if scale < 1.0:
        logo = logo.resize(
            (max(1, int(logo.width * scale)), max(1, int(logo.height * scale))),
            Image.LANCZOS,
        )
    x = (tile_width - logo.width) // 2
    y = (tile_height - logo.height) // 2
    tile.alpha_composite(logo, (x, y))
    return tile


def build_tilted_grid(tiles, canvas_width, canvas_height, scale=1.0, focus_x=None, focus_y=None):
    fx = FOCUS_X if focus_x is None else focus_x
    fy = FOCUS_Y if focus_y is None else focus_y

    tile_width = int(TILE_W * scale)
    tile_height = int(TILE_H * scale)
    gap = int(GAP * scale)

    cols = COLS + 3
    rows = ROWS + 3
    needed = rows * cols
    tile_list = (tiles * (needed // len(tiles) + 1))[:needed]
    stagger_px = int(STAGGER * (tile_width + gap))

    grid_width = cols * (tile_width + gap) + rows * stagger_px
    grid_height = rows * (tile_height + gap)
    grid = Image.new("RGBA", (grid_width, grid_height), (0, 0, 0, 0))

    focal_x = fx * grid_width
    focal_y = fy * grid_height
    focal_row = max(0, min(rows - 1, int(focal_y / (tile_height + gap))))
    focal_col = max(0, min(cols - 1, int((focal_x - focal_row * stagger_px) / (tile_width + gap))))

    cells = [(row, col) for row in range(rows) for col in range(cols)]
    cells.sort(key=lambda pos: abs(pos[0] - focal_row) + abs(pos[1] - focal_col))

    for index, (row, col) in enumerate(cells):
        if index >= len(tile_list):
            break
        x = row * stagger_px + col * (tile_width + gap)
        y = row * (tile_height + gap)
        image, title, logo = tile_list[index]
        tile = make_tile(image, tile_width, tile_height)
        tile = overlay_clear_logo(tile, logo)
        grid.paste(tile, (x, y), tile)

    rotated = grid.rotate(TILT_DEG, expand=True, resample=Image.BICUBIC)
    rotated_width, rotated_height = rotated.size

    angle_rad = math.radians(-TILT_DEG)
    pre_center_x = fx * grid_width - grid_width / 2
    pre_center_y = fy * grid_height - grid_height / 2
    rot_center_x = pre_center_x * math.cos(angle_rad) - pre_center_y * math.sin(angle_rad)
    rot_center_y = pre_center_x * math.sin(angle_rad) + pre_center_y * math.cos(angle_rad)

    focus_in_rot_x = rotated_width / 2 + rot_center_x
    focus_in_rot_y = rotated_height / 2 + rot_center_y

    paste_x = int(canvas_width / 2 - focus_in_rot_x)
    paste_y = int(canvas_height / 2 - focus_in_rot_y)

    canvas = Image.new("RGBA", (canvas_width, canvas_height), (10, 10, 12, 255))
    canvas.paste(rotated, (paste_x, paste_y), rotated)
    return canvas


def ensure_minimum_tiles(tiles, minimum_count):
    """Repeat available (image, title) tiles until the minimum count is met."""
    if len(tiles) >= minimum_count or not tiles:
        return tiles

    padded = list(tiles)
    for tile in itertools.cycle(tiles):
        if len(padded) >= minimum_count:
            break
        padded.append(tile)
    return padded


def apply_gradient(canvas, accent):
    width, height = canvas.size

    def make_linear_gradient(grad_width, grad_height, direction):
        image = Image.new("RGBA", (grad_width, grad_height), (0, 0, 0, 0))
        pixels = image.load()

        if direction == "left":
            for x in range(grad_width):
                mix = max(0.0, 1.0 - x / (grad_width * 0.45))
                alpha = int(200 * mix ** 1.6)
                if alpha:
                    color = (6, 6, 8, alpha)
                    for y in range(grad_height):
                        pixels[x, y] = color

        elif direction == "bottom":
            for y in range(grad_height):
                mix = max(0.0, (y - grad_height * 0.50) / (grad_height * 0.50))
                alpha = int(200 * mix ** 1.4)
                if alpha:
                    color = (6, 6, 8, alpha)
                    for x in range(grad_width):
                        pixels[x, y] = color

        elif direction == "corner_bl":
            max_diag = math.hypot(grad_width, grad_height)
            for x in range(grad_width):
                for y in range(grad_height):
                    distance = math.hypot(x, grad_height - y)
                    mix = distance / max_diag
                    base = max(0.0, 1.0 - mix / 0.60)
                    alpha = int(230 * base ** 2.2)
                    if alpha:
                        pixels[x, y] = (6, 6, 8, min(255, alpha))

        elif direction == "corner_tr_color":
            max_diag = math.hypot(grad_width, grad_height)
            red, green, blue = accent
            for x in range(grad_width):
                for y in range(grad_height):
                    distance = math.hypot(grad_width - x, y)
                    mix = distance / max_diag
                    base = max(0.0, 1.0 - mix / 0.72)
                    alpha = int(118 * base ** 1.9)
                    if alpha:
                        pixels[x, y] = (red, green, blue, min(255, alpha))

        return image

    left_grad = make_linear_gradient(width, height, "left")
    bottom_grad = make_linear_gradient(width, height, "bottom")
    small_corner = make_linear_gradient(width // 4, height // 4, "corner_bl")
    corner_grad = small_corner.resize((width, height), Image.BILINEAR)
    accent_small = make_linear_gradient(width // 4, height // 4, "corner_tr_color")
    accent_grad = accent_small.resize((width, height), Image.BILINEAR)

    result = Image.alpha_composite(canvas, corner_grad)
    result = Image.alpha_composite(result, left_grad)
    result = Image.alpha_composite(result, bottom_grad)
    accent_grad = accent_grad.filter(ImageFilter.GaussianBlur(radius=max(28, width // 64)))
    return Image.alpha_composite(result, accent_grad)


def composite_logo(canvas, logo_path, max_width_ratio=0.42, y_ratio=0.42):
    """Overlay a clear logo (transparent PNG) centered on the backdrop."""
    if not logo_path:
        return canvas
    logo_path = Path(logo_path)
    if not logo_path.exists():
        return canvas

    logo = Image.open(logo_path).convert("RGBA")
    bbox = logo.getchannel("A").getbbox()
    if not bbox:
        return canvas
    logo = logo.crop(bbox)

    target_width = int(canvas.width * max_width_ratio)
    if logo.width > target_width:
        scale = target_width / logo.width
        logo = logo.resize((target_width, int(logo.height * scale)), Image.LANCZOS)

    x = (canvas.width - logo.width) // 2
    y = int(canvas.height * y_ratio) - logo.height // 2
    y = max(0, min(y, canvas.height - logo.height))

    # Soft drop shadow so the logo stays readable over bright tiles.
    pad = 12
    shadow = Image.new("RGBA", (logo.width + pad * 2, logo.height + pad * 2), (0, 0, 0, 0))
    shadow_alpha = logo.getchannel("A").resize(shadow.size, Image.LANCZOS)
    shadow.putalpha(shadow_alpha)
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    canvas.alpha_composite(shadow, (x - pad, y - pad))
    canvas.alpha_composite(logo, (x, y))
    return canvas


def resolve_quality_settings(profile="compressed", quality=None):
    settings = dict(QUALITY_PRESETS[profile])
    if quality is not None:
        settings["quality"] = quality
    return settings


def save_output(canvas, path, quality_settings):
    final = canvas.convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    final.save(
        path,
        "JPEG",
        quality=quality_settings["quality"],
        optimize=True,
        progressive=quality_settings["progressive"],
        subsampling=quality_settings["subsampling"],
    )
    jpg_size_mb = os.path.getsize(path) / 1_048_576
    print(
        f"  Saved {path} ({final.size[0]}x{final.size[1]}, {jpg_size_mb:.1f} MB, "
        f"q={quality_settings['quality']}, mode={quality_settings['subsampling']})"
    )
    webp_path = path.with_suffix(".webp")
    with Image.open(path) as jpg_image:
        jpg_image.save(webp_path, "WEBP", quality=quality_settings["quality"], method=6)
        webp_size_mb = os.path.getsize(webp_path) / 1_048_576
        print(
            f"  Saved {webp_path} ({jpg_image.size[0]}x{jpg_image.size[1]}, {webp_size_mb:.1f} MB, "
            f"q={quality_settings['quality']})"
        )


def resolve_outputs(output=None, output_dir=None, label=None, size="both"):
    if output:
        base = Path(output)
        if size == "both":
            return {
                "4k": base.with_name(f"{base.stem}_4k{base.suffix or '.jpg'}"),
                "1080p": base.with_name(f"{base.stem}_1080p{base.suffix or '.jpg'}"),
            }
        return {size: base}

    directory = Path(output_dir or DEFAULT_OUTPUT_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    stem = (label or "backdrop").strip().lower().replace(" ", "_").replace("/", "_")
    if size == "both":
        return {
            "4k": directory / f"{stem}_wallpaper_4k.jpg",
            "1080p": directory / f"{stem}_wallpaper_1080p.jpg",
        }
    suffix = "4k" if size == "4k" else "1080p"
    return {size: directory / f"{stem}_wallpaper_{suffix}.jpg"}


def backdrops(
    api_key,
    label,
    tmdb_requests,
    fanart_key=None,
    mdblist_key=None,
    accent_color=None,
    output=None,
    output_dir=None,
    focus_x=None,
    focus_y=None,
    count=60,
    size="both",
    profile="compressed",
    quality=None,
    preferred_language="en",
    logo=None,
    logger=None,
):
    """Fetch titles for the supplied TMDB requests and render one or more backdrop images."""
    log = logger or print
    request_specs = [parse_request_spec(spec) for spec in tmdb_requests]
    if not request_specs:
        raise ValueError("No TMDB request specs were supplied.")

    fx = FOCUS_X if focus_x is None else focus_x
    fy = FOCUS_Y if focus_y is None else focus_y
    accent = accent_color or default_accent_for_label(label)
    outputs = resolve_outputs(output=output, output_dir=output_dir, label=label, size=size)
    quality_settings = resolve_quality_settings(profile=profile, quality=quality)

    fanart_note = "Fanart.tv thumbs" if fanart_key else "TMDB backdrops only"
    log(f"\n{'-' * 50}")
    log(f"  Label   : {label}")
    log(f"  Images  : {fanart_note}")
    log(f"  Lang    : preferred={preferred_language}")
    log(f"  Focus   : x={fx:.2f}, y={fy:.2f}")
    log(f"  Sizes   : {', '.join(outputs)}")
    log(f"  Profile : {profile} (q={quality_settings['quality']})")
    log(f"{'-' * 50}\n")

    log("Fetching titles...")
    titles = fetch_titles(request_specs, api_key, count=count, mdblist_key=mdblist_key)
    log(f"  Found {len(titles)} titles.\n")
    if not titles:
        raise RuntimeError("No titles found for the supplied TMDB requests.")

    log("Downloading tile images...")
    tile_images = []
    tile_titles = []
    tile_logos = []
    fanart_hits = 0
    tmdb_fallbacks = 0
    other_language_fanart_hits = 0
    progress_output = io.StringIO()
    show_tty_progress = sys.stdout.isatty()
    for index, (media_type, item) in enumerate(titles, start=1):
        title = item.get("title") or item.get("name", "?")
        progress_line = f"  [{index:02d}/{len(titles)}] {title[:40]:<40}"
        if show_tty_progress:
            sys.stdout.write(f"{progress_line}\r")
            sys.stdout.flush()
        else:
            log(progress_line)
        image, source = fetch_tile_image(media_type, item, api_key, fanart_key, preferred_language)
        if not image:
            continue
        tile_logo = fetch_tmdb_logo(media_type, item["id"], api_key)
        if tile_logo is None:
            log(f"    skipping {title}: no clear logo available")
            continue
        tile_images.append(image)
        tile_titles.append(title)
        tile_logos.append(tile_logo)
        if source == "fanart":
            fanart_hits += 1
        elif source == "fanart_other_language":
            other_language_fanart_hits += 1
        else:
            tmdb_fallbacks += 1
    if show_tty_progress and titles:
        sys.stdout.write("\n")
        sys.stdout.flush()

    if fanart_key:
        log(
            f"  Downloaded {len(tile_images)} images "
            f"({fanart_hits} preferred/original/textless Fanart, "
            f"{tmdb_fallbacks} TMDB fallback, "
            f"{other_language_fanart_hits} other-language Fanart).\n"
        )
    else:
        log(f"  Downloaded {len(tile_images)} images.\n")

    tile_pairs = list(zip(tile_images, tile_titles, tile_logos))
    if not tile_pairs:
        raise RuntimeError("No tiles with clear logos could be downloaded for the supplied TMDB requests.")
    minimum_tiles = 12
    if len(tile_pairs) < minimum_tiles:
        log(f"  Only {len(tile_pairs)} image(s) available; repeating tiles to reach {minimum_tiles}.\n")
        tile_pairs = ensure_minimum_tiles(tile_pairs, minimum_tiles)

    saved_paths = {}
    for output_size, destination in outputs.items():
        width, height, scale = SIZE_PRESETS[output_size]
        log(f"Compositing {output_size} ({width}x{height})...")
        canvas = build_tilted_grid(tile_pairs, width, height, scale=scale, focus_x=fx, focus_y=fy)
        canvas = apply_gradient(canvas, accent)
        canvas = composite_logo(canvas, logo)
        with contextlib.redirect_stdout(progress_output):
            save_output(canvas, destination, quality_settings=quality_settings)
        for line in progress_output.getvalue().splitlines():
            if line.strip():
                log(line)
        progress_output.seek(0)
        progress_output.truncate(0)
        saved_paths[output_size] = destination

    log("\nDone.\n")
    return saved_paths


def parse_focus_value(value):
    if not value:
        return FOCUS_X, FOCUS_Y
    if value in FOCUS_PRESETS:
        return FOCUS_PRESETS[value]
    try:
        raw_x, raw_y = value.split(",", 1)
        return float(raw_x), float(raw_y)
    except Exception as exc:
        raise ValueError(
            f"Invalid --focus value '{value}'. Use a preset ({', '.join(FOCUS_PRESETS)}) or 'x,y'."
        ) from exc


def apply_layout_overrides(args):
    """Apply runtime layout overrides from CLI flags to the module constants."""
    global CARD_RADIUS, TILT_DEG, TILE_W, TILE_H, GAP, STAGGER
    if args.tile_width is not None:
        TILE_W = args.tile_width
    if args.tile_height is not None:
        TILE_H = args.tile_height
    if args.tilt_deg is not None:
        TILT_DEG = args.tilt_deg
    if args.gap is not None:
        GAP = args.gap
    if args.stagger is not None:
        STAGGER = args.stagger
    if args.card_radius is not None:
        CARD_RADIUS = args.card_radius


def main():
    parser = argparse.ArgumentParser(description="Generate collection backdrops from explicit TMDB requests.")
    parser.add_argument("--api-key", required=False, help="TMDB API key (v3)")
    parser.add_argument("--fanart-key", required=False, default=None, help="Fanart.tv API key")
    parser.add_argument("--mdblist-key", required=False, default=None, help="MDBList API key for mdblist.* catalogs")
    parser.add_argument("--preferred-language", default="en", help="Preferred Fanart artwork language code. Default: en")
    parser.add_argument("--label", required=True, help="Label for logs and fallback accent generation")
    parser.add_argument(
        "--tmdb-request",
        action="append",
        default=[],
        help="TMDB request spec. Repeat this flag to merge multiple catalogs into one backdrop.",
    )
    parser.add_argument("--accent-color", default=None, help="Accent color as '#RRGGBB' or 'R,G,B'")
    parser.add_argument("--logo", default=None, help="Path to a clear logo PNG to overlay on the backdrop")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for generated files when --output is not set")
    parser.add_argument("--output", default=None, help="Exact output file path. Use this when another script has already decided the final filename.")
    parser.add_argument("--size", choices=("4k", "1080p", "both"), default="both", help="Which size(s) to render")
    parser.add_argument("--profile", choices=tuple(QUALITY_PRESETS), default="compressed", help="Named output profile. 'compressed' is smaller, 'high' keeps more detail.")
    parser.add_argument("--quality", type=int, default=None, help="Advanced override for output quality from 1-95. If set, it overrides the selected profile for both JPG and WEBP.")
    parser.add_argument(
        "--focus",
        default=None,
        help=f"Preset ({', '.join(FOCUS_PRESETS)}) or 'x,y' fractions for focal placement.",
    )
    parser.add_argument("--count", type=int, default=60, help="Max number of source titles to use after merging requests")
    parser.add_argument("--tile-width", type=int, default=None, help="Override tile width in pixels")
    parser.add_argument("--tile-height", type=int, default=None, help="Override tile height in pixels")
    parser.add_argument("--tilt-deg", type=float, default=None, help="Override grid rotation in degrees")
    parser.add_argument("--gap", type=int, default=None, help="Override gap between tiles in pixels")
    parser.add_argument("--stagger", type=float, default=None, help="Override row stagger as a fraction of the tile step")
    parser.add_argument("--card-radius", type=int, default=None, help="Override tile corner radius in pixels")
    args = parser.parse_args()

    if not args.api_key:
        print("Error: --api-key is required.")
        sys.exit(1)

    try:
        accent = parse_accent_color(args.accent_color) if args.accent_color else None
        focus_x, focus_y = parse_focus_value(args.focus)
        if args.quality is not None and (args.quality < 1 or args.quality > 95):
            raise ValueError("--quality must be between 1 and 95.")
        apply_layout_overrides(args)
        backdrops(
            api_key=args.api_key,
            label=args.label,
            tmdb_requests=args.tmdb_request,
            fanart_key=args.fanart_key,
            mdblist_key=args.mdblist_key,
            accent_color=accent,
            output=args.output,
            output_dir=args.output_dir,
            focus_x=focus_x,
            focus_y=focus_y,
            count=args.count,
            size=args.size,
            profile=args.profile,
            quality=args.quality,
            preferred_language=args.preferred_language,
            logo=args.logo,
        )
    except Exception as exc:
        print(f"Error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup_pycache()
