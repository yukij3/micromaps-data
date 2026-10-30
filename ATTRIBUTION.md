# Attribution

## Map data

Map data © OpenStreetMap contributors, available under the Open Database License (ODbL).
https://www.openstreetmap.org/copyright

Snapshot dates are the `date` of each pack in `catalog.json`. Roads, trails, water, peaks, huts and
place names are filtered and simplified from OpenStreetMap (`classes` in FORMAT.md section 4).

## Terrain

Contour lines and elevation grids are computed from Terrain Tiles by Mapzen, hosted on AWS
(https://registry.opendata.aws/terrain-tiles/). The sources behind each area are read from the
`x-amz-meta-x-imagery-sources` header of every tile used and listed as `terrain` in `catalog.json`.

- ArcticDEM: DEM(s) courtesy of the Polar Geospatial Center. DEM(s) were created from DigitalGlobe,
  Inc., imagery and funded under National Science Foundation awards 1043681, 1559691, and 1542736.
- GMTED2010: data courtesy of the U.S. Geological Survey.
- SRTM: data courtesy of NASA and the U.S. Geological Survey.
- EU-DEM: produced using Copernicus data and information funded by the European Union.
- ETOPO1: data courtesy of the NOAA National Centers for Environmental Information.
- Full list of providers: https://github.com/tilezen/joerd/blob/master/docs/attribution.md

Elevation is a separate layer and is not derived from OpenStreetMap. It is published here together
with the map data because it is stored in the same tiles.
