# microMAPS map data

Free, machine-readable map packs for the microMAPS watch app (Garmin Instinct 3 Solar), and the
static page that goes with it. Pick a block of map anywhere in the world or add a GPX route at
https://yukij3.github.io/micromaps-data/

- Download list: `catalog.json`
- Each pack: `packs/<id>/<date>/`
  - `manifest.bin`, `tiles.bin`: the database in the MMAP format
  - `route.bin`: built-in demo route (Khibiny only)
  - `4k/`, `2k/`: the same data cut into groups for download by the watch
- Format: [FORMAT.md](FORMAT.md). Reader: `tools/decode.py` (`python3 -m tools.decode packs/khibiny/2026-10-28`
  from the repository root; `dem.py` is its helper for the elevation grids).
- Blocks built on request: `blocks/index.json` lists them, each one is in `blocks/<id>/`

A published pack directory never changes. A new snapshot is a new `<date>`.

## How the watch gets a pack

The watch itself requests `catalog.json`, then `pack.json` and the group files of the chosen pack,
over HTTPS through Garmin Connect on the paired phone. Files are served by jsDelivr, with GitHub Pages
as the fallback. These services see an IP address and which file was requested. The app has no
account, no analytics and no server of its own.

## Licence

The map data in `packs/` and `blocks/` is derived from OpenStreetMap and is made available under the
**Open Database License 1.0** ([LICENSE-ODbL.txt](LICENSE-ODbL.txt)). Individual contents of the
database are under the Database Contents License 1.0 ([LICENSE-DbCL.txt](LICENSE-DbCL.txt)).

Map data © OpenStreetMap contributors, https://www.openstreetmap.org/copyright

See [ATTRIBUTION.md](ATTRIBUTION.md) for the terrain sources of each area.

The page (`index.html`, `app.js`, `style.css`, `blocks.js`, `route/`) and the tools in `tools/` are MIT licensed. `vendor/` holds Leaflet 1.9.4 from
npm, under its BSD-2 licence ([vendor/LICENSE-leaflet.txt](vendor/LICENSE-leaflet.txt)).
