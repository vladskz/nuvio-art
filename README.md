# Nuvio Art

Image assets for the Nuvio collection setup (`catalogs.json`), plus the
automation that regenerates **dynamic backdrops** from TMDB every month.

## Structure

```
collections/
├── scripts/                 # backdrop generation + cache purge
│   ├── backdrops.py         # wrapper: reads templates, one job per folder
│   ├── backdrop.py          # collage renderer (layout constants live here)
│   ├── accent.py            # derives an accent color from a cover image
│   └── purge.py             # invalidates jsDelivr cache after updates
├── discover/                # 🔭 Discover: popular, trending, top-rated
├── streaming/               # 🎬 Streaming: netflix, disney-plus, ...
├── genres/                  # 🎭 Genres: action, animation, ...
├── themes/                  # 💡 Themes
├── decades/                 # 📅 Decades
└── runtime/                 # ⏱️ Runtime buckets
    └── <group>/
        ├── cover/           # cover images (.jpg / .png)
        ├── focused/         # focus GIFs (.gif)
        ├── backdrop/        # generated backdrops (.jpg + .webp)
        └── logo/            # clear logos
templates/
├── Nuvio-Collections.json   # folder definitions (copy of catalogs.json)
└── AIOMetadata.json         # catalog id -> TMDB discover params
.github/workflows/
└── monthly-backdrops.yml    # scheduled regeneration
```

## How backdrops are generated

A GitHub Actions workflow (`monthly-backdrops.yml`) runs on the **1st of every
month** (and on demand) and:

1. reads `templates/Nuvio-Collections.json` for every folder,
2. resolves each folder's catalog sources to TMDB queries via
   `templates/AIOMetadata.json`,
3. fetches the current top titles and builds a landscape collage per folder,
4. commits the new `collections/<group>/backdrop/<slug>.jpg` + `.webp` files,
5. purges the jsDelivr cache so the new images go live immediately.

### Setup

1. Add your TMDB API key as a repo secret named `TMDB_API_KEY`
   (Settings → Secrets and variables → Actions → New repository secret).
   Get one free at https://www.themoviedb.org/settings/api.
2. (Optional) Add a Fanart.tv key as `FANART_API_KEY` for better tile art.
3. Run the workflow once manually:
   **Actions → Monthly Backdrops → Run workflow**.

### Run locally

```bash
pip install pillow requests
python -B collections/scripts/backdrops.py \
  --api-key YOUR_TMDB_KEY \
  --profile compressed \
  --parallelism 3
```

Add `--dry-run` to preview the resolved folders and TMDB requests without
downloading images, or `--catalog streaming/netflix` to target one folder.

## Customising the collage

The look is controlled by constants at the top of
`collections/scripts/backdrop.py`:

| Constant | Default | Meaning |
|----------|---------|---------|
| `TILE_W` / `TILE_H` | `560` / `315` | tile size (landscape 16:9) |
| `TILT_DEG` | `0` | grid rotation in degrees |
| `STAGGER` | `0.5` | horizontal row offset |
| `GAP` | `16` | gap between tiles |
| `CARD_RADIUS` | `12` | tile corner radius |
| `ROWS` / `COLS` | `9` / `9` | source grid size |
| `FOCUS_X` / `FOCUS_Y` | `0.5` / `0.53` | focal point of the visible crop |

You can also override them per run with `--tile-width`, `--tile-height`,
`--tilt-deg`, `--gap`, `--stagger`, `--card-radius`.

## Wiring backdrops into Nuvio

Each folder in your `catalogs.json` needs a `heroBackdropUrl` (and optionally
`titleLogoUrl`) pointing at the generated asset:

```json
"heroBackdropUrl": "https://cdn.jsdelivr.net/gh/vladskz/nuvio-art/collections/streaming/backdrop/netflix.webp"
```

The `templates/Nuvio-Collections.json` in this repo already includes the
`heroBackdropUrl` for every folder as a ready reference.

## Naming

- Use **kebab-case** matching the catalog ID (e.g. `disney-plus`, `apple-tv`).
- `cover/` → `coverImageUrl`, `focused/` → `focusGifUrl`,
  `backdrop/` → `heroBackdropUrl`, `logo/` → `titleLogoUrl`.
