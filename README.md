# Nuvio Art

Image assets for the Nuvio collection setup (`catalogs.json`).

## Structure

```
collections/
├── discover/          # 🔭 Discover: popular, trending, top-rated
│   ├── cover/         # collection cover images (.jpg / .png)
│   ├── focused/       # focus GIFs (.gif)
│   ├── backdrop/      # backdrop images
│   └── logo/          # clear logos
├── streaming/         # 🎬 Streaming: netflix, disney-plus, ...
│   ├── cover/
│   ├── focused/
│   ├── backdrop/
│   └── logo/
└── genres/            # 🎭 Genres: action, animation, ...
    ├── cover/
    ├── focused/
    ├── backdrop/
    └── logo/
```

## Naming

- Use **kebab-case** matching the catalog ID in `catalogs.json`
  (e.g. `disney-plus`, `prime-video`, `apple-tv`).
- `cover/` → `coverImageUrl`
- `focused/` → `focusGifUrl`
- `backdrop/` → backdrop images
- `logo/` → clear logos

## Current assets

| File | Catalog |
|------|---------|
| `collections/streaming/cover/netflix.png` | Netflix |
| `collections/streaming/cover/disney-plus.png` | Disney Plus |
| `collections/streaming/cover/prime-video.png` | Prime Video |
| `collections/streaming/cover/apple-tv.png` | Apple TV |
| `collections/streaming/cover/hbo-max.png` | HBO Max |
| `collections/streaming/cover/sky-showtime.png` | Sky Showtime |

## Using via jsDelivr

Once pushed to GitHub, reference images like:

```
https://cdn.jsdelivr.net/gh/<user>/<repo>/collections/streaming/cover/netflix.png
```
