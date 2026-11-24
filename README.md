# Nityaradhya temple data

Open data and tools for the temple directory of the Nityaradhya app.

- This repository holds only the tool that collects public Hindu temple facts
  (name, place, map point) and, later, the finished directory files.
- It holds no app code, no keys and no personal data.

## Data sources and licences

- OpenStreetMap: © OpenStreetMap contributors, Open Database Licence (ODbL) 1.0.
  https://www.openstreetmap.org/copyright
- Wikidata: CC0 1.0.
- Any database published here that is derived from OpenStreetMap is shared under ODbL 1.0.

## How the data is collected

`tools/temples/bulk_osm.py` reads public OpenStreetMap extracts and keeps only
places of worship tagged as Hindu. See `tools/temples/RUN_BULK.md`.
The job in `.github/workflows/fetch-temple-data.yml` is started by hand only.
