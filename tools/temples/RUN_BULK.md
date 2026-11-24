# Bulk temple facts — Linux guide

Run this from the repository folder:

```sh
python3 tools/temples/bulk_osm.py
```

No arguments are needed. The folder `bulk-osm-out/` contains the result.
This command does not build the app, sign anything, upload anything, or start
GitHub Actions. Claude must write and obtain approval for any manual hosted job.

Before downloading, view the saved plan without any network request:

```sh
python3 tools/temples/bulk_osm.py --dry-run
```

The working limit is 28 minutes. A hard timer allows at most 15 more seconds to
stop workers and close the manifest. This is a **time limit**, not a promise that
every country can be downloaded in one run. Read each country's status.

The script tries one country-area Overpass query, including lifecycle/heritage
tags, then a Geofabrik fallback. India uses Geofabrik first. Large countries use
Geofabrik child extracts; they never download their large national extract.
Countries with extract fallbacks are ordered by estimated size, then code.
Overpass-only countries follow them. Child extracts use size then identifier. Actual HEAD sizes,
free disk, remaining time and observed transfer speed can stop a download.
India's budget is reserved while smaller countries are tried first.
The UK fallback includes the shared Ireland extract for Northern Ireland;
Bermuda and the Falklands are not treated as UK subregions. US state extracts
are used without repeating the overlapping five broad regional extracts.

Ubuntu needs `osmium-tool` and the Python `osmium` package (pyosmium). If missing,
the script logs and attempts `apt-get` plus a temporary Python virtual
environment. The machine needs root or password-free sudo for apt. No secrets
are used. Tool installation time counts against the same limit.

On a machine where these tools are already installed, use `--no-install`.
`TEMPLES_OSMIUM_PYTHON` may name an existing Python with pyosmium installed.
Neither the installed tools nor their caches belong in the patch ZIP.

To finish a particular country later:

```sh
python3 tools/temples/bulk_osm.py --regions IN
python3 tools/temples/bulk_osm.py --regions NP,LK,MY,SG,FJ,MU
```

Run from the same folder to resume. Completed files are checked by their
**decoded** SHA-256 and reused. Completed sub-extracts are reused too. A verified shared extract covering several
countries is not downloaded again for each of those countries. A previous
failed Overpass attempt proceeds straight to its extract fallback on resume.
To deliberately try Overpass again, use a new output folder with `--out`.
There is only one network request at a time, with a pause between area queries.
After two HTTP 429 responses the script stops Overpass for that run. It does
not rotate servers to escape a rate limit.

The original PBF is deleted after filtering and before centre extraction.
The next extract starts only after the temporary workspace is removed.
Download temporary files live under `bulk-osm-out/.work/`, so its filesystem
must have enough free disk. India currently needs about 7 GB of headroom under
the conservative guard. Extracts over 1,800 MiB are skipped.

Results:

- `manifest.json`: URLs, source dates, hashes, counts, country/sub-extract
  completion and every failed attempt. `complete: false` is a normal partial
  result, not a claim of world completeness.
- `facts/*.json.gz`: object identities, unchanged allowed source tags and
  node positions / bounding-box centres. No descriptions, photos, contact or
  opening-hour tags. Nodes, ways and relations remain distinguishable.
- Parts are below 19 MB. Facts plus manifest stop below 149 MB, with room reserved for provenance.
  Downloaded extracts and filtered PBFs are temporary, not deliverables.

Hand the whole output folder to Codex. It may contain useful completed
countries/sub-extracts even when other countries failed. Do not hand over the
temporary `.work/` folder if a machine was forcibly powered off.

Import and rebuild **offline**:

```sh
python3 tools/temples/bulk_input.py /path/to/bulk-osm-out
python3 tools/temples/pipeline.py --out /path/to/new-directory --registry content/temples-directory/registry.json
```

The importer checks the entire incoming manifest before copying inputs. It
registers `osm-bulk` with source URL, date, licence and decoded hashes. It never
treats an unfinished country as complete. Fully completed sub-extracts can be
used independently; their country stays incomplete in the evidence manifest.
An extract boundary is not a country claim: Geofabrik includes a border buffer.

At over 100,000 registered bulk rows the builder selects temporary SQLite row
stores automatically. To exercise that route on smaller saved inputs:

```sh
TEMPLES_STREAM=1 python3 tools/temples/pipeline.py --out /tmp/directory-check
```

Synthetic stress test (output is deleted; only measurements survive):

```sh
python3 tools/temples/stress.py --rows 250000 --report /tmp/temples-stress.json
```

Licences and references:

- OpenStreetMap / Geofabrik facts: ODbL 1.0. Credit “© OpenStreetMap
  contributors”, link the licence, and offer the derived database under ODbL.
- <https://www.geofabrik.de/data/download.html>
- <https://download.geofabrik.de/technical.html>
- <https://overpass.private.coffee/index.html>
- <https://docs.osmcode.org/osmium/latest/osmium-tags-filter.html>
- <https://docs.osmcode.org/pyosmium/latest/reference/Handler-Processing/>

The Android app remains offline. This is a data preparation tool only.
