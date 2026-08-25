# Phase 6 — QGIS ↔ PostGIS

Goal: open the demo lane in QGIS, see the four layers, edit them, and have
every edit land straight back in `wastraq_demo`. Once that loop works you can
delete the synthetic geometry and draw the real one-lane map in its place —
the backend doesn't change at all.

## 0. Install QGIS

```bash
brew install --cask qgis
```

Open it from Applications (first launch takes a while; macOS may ask you to
allow it under System Settings → Privacy & Security).

## 1. Add a PostgreSQL connection

1. In the **Browser** panel (left), right-click **PostgreSQL** → **New Connection…**
2. Fill in:

   | Field | Value |
   |---|---|
   | Name | `wastraq_demo` |
   | Host | `localhost` |
   | Port | `5432` |
   | Database | `wastraq_demo` |
   | SSL mode | `disable` |
   | Authentication | **Basic** tab → User name = your macOS username (`whoami`) |

   Leave the password blank — a Homebrew PostgreSQL trusts local connections.
3. Tick **Also list tables with no geometry** (so `properties`, `pickers`,
   `collection_events` and `evidence` show up too).
4. Click **Test Connection** → you should get *Connection to wastraq_demo was successful*.
5. **OK**.

If Test Connection fails:

```bash
pg_isready                      # is the server up?
brew services restart postgresql@17
psql -d wastraq_demo -c "select 1"   # does your user have access?
```

## 2. Load the layers

Expand **PostgreSQL → wastraq_demo → public**. Drag these into the map canvas:

| Layer | Geometry | What it is |
|---|---|---|
| `property_service_zones` | Polygon | the association surface — the layer that matters |
| `property_frontages` | Line | road-facing edge of each plot |
| `property_entrances` | Point | where waste actually leaves the property |
| `properties` | no geometry | attributes; opens as a table |

Order them polygon → line → point (bottom to top) so the points stay visible.

**Basemap for context** — Browser → **XYZ Tiles** → **OpenStreetMap**, drag it in
and move it to the bottom. The real lane sits near 12.29433 N, 76.64148 E (2nd Cross Road, Krishnamurthy
Puram, Mysuru); use **Zoom to Layer** on `property_service_zones` to get there.

Set the project CRS to **EPSG:4326** (bottom-right of the window) so what you draw
matches what the database stores.

### Make the zones readable

Right-click `property_service_zones` → **Properties**:

- **Symbology** → Simple fill → set fill to ~30 % opacity so the basemap shows through.
- **Labels** → Single labels → Value = `property_id`.

## 3. Edit existing geometry

1. Select the layer.
2. Click the **yellow pencil** (Toggle Editing), or `Ctrl+E`.
3. **Vertex Tool** (`Ctrl+Shift+V`) — drag a corner of a service zone.
4. **Save Layer Edits** (the floppy icon), then toggle editing off.
5. Confirm it actually reached PostGIS:

```bash
psql -d wastraq_demo -c \
  "SELECT zone_id, round(ST_Area(geometry::geography)) AS m2 FROM property_service_zones ORDER BY zone_id;"
```

The area changes immediately — QGIS writes through, there is no export step.

## 4. Add a new property (the real workflow)

The tables have foreign keys, so create the row first, then the geometry.

**4a. Create the property row**

Easiest from psql:

```bash
psql -d wastraq_demo <<'SQL'
INSERT INTO properties (property_id, authority_property_id, house_number, owner_name,
                        formatted_address, property_type, route_id,
                        mapping_confidence, verification_status)
VALUES ('PROP-011','ULB-DM-1011','12/11','New Owner',
        '12/11, Demo Lane, Ward 42, Demo City 560001','RESIDENTIAL','ROUTE-A',
        0.900,'UNVERIFIED');
SQL
```

Or in QGIS: open the `properties` table, toggle editing, **Add Record**, fill the
row, save.

**4b. Draw the entrance point**

1. Select `property_entrances` → Toggle Editing.
2. **Add Point Feature** (`Ctrl+.`), click where the bins actually go out.
3. In the attribute dialog: `entrance_id` = `ENT-011`, `property_id` = `PROP-011`,
   `verified` = true. Leave `created_at` blank — the default fills it.
4. Save Layer Edits.

**4c. Draw the frontage line**

1. Select `property_frontages` → Toggle Editing.
2. **Add Line Feature** (`Ctrl+.`), click along the plot's road-facing edge,
   right-click to finish.
3. `frontage_id` = `FRONT-011`, `property_id` = `PROP-011`, `road_side` = one of
   `NORTH` / `SOUTH` / `EAST` / `WEST` (the CHECK constraint rejects anything else).
4. Save.

**4d. Draw the service-zone polygon**

1. Select `property_service_zones` → Toggle Editing.
2. **Add Polygon Feature**, click the corners of the area where that property's
   waste is realistically collected — the strip between the frontage and the
   kerb — right-click to close.
3. `zone_id` = `SZ-011`, `property_id` = `PROP-011`, `version` = `1`,
   `verified` = true.
4. Save Layer Edits, toggle editing off.

**Snapping makes adjacent zones line up.** Turn on the magnet icon
(**Project → Snapping Options**), snap to *Vertex and Segment* at ~12 px, and
enable **Avoid Overlap** on the layer — the demo's ambiguity logic depends on
zones not overlapping.

**4e. Check the engine sees it**

```bash
# use a coordinate inside the polygon you just drew
psql -d wastraq_demo -c "SELECT * FROM wastraq_lookup_property(<lat>, <lon>);"

curl -s -X POST http://127.0.0.1:8000/gis/lookup \
  -H 'content-type: application/json' \
  -d '{"latitude": <lat>, "longitude": <lon>}' | python3 -m json.tool
```

You should get `AUTO_ASSOCIATED` with `PROP-011`. Nothing in the backend needed
changing — that is the whole point of keeping association in the GIS layer.

## 5. After loading the real 16-property lane - refresh and inspect

Your PostGIS connection is unchanged, so **do not recreate it**. The three
layers already point at the same tables; they just need to re-read them.

**Refresh the three layers**

1. Bring QGIS to the front. If any layer is in edit mode, click the yellow
   pencil to leave it (save or discard first) - a layer being edited will not
   reload.
2. Select all three layers in the Layers panel (click `property_entrances`,
   then Cmd-click `property_frontages` and `property_service_zones`).
3. Right-click -> **Reload Layer**. (Keyboard: `F5` reloads the selected
   layers. If the panel still looks stale, **View -> Refresh** / `Cmd+R`
   redraws the canvas.)

If a layer shows nothing at all, its saved extent is still on the old Bengaluru
lane - that is what the next step fixes.

**Zoom to Layer**

4. Right-click `property_service_zones` -> **Zoom to Layer**.
   You should land on 2nd Cross Road, Krishnamurthy Puram, Mysuru
   (~12.29433 N, 76.64148 E). Add **XYZ Tiles -> OpenStreetMap** underneath if
   you want street context.
5. Set the project CRS to **EPSG:4326** (bottom-right) so what you draw matches
   what is stored.

**Visually inspect the 16 real properties**

6. Label the zones: right-click `property_service_zones` -> **Properties ->
   Labels -> Single labels**, Value = `property_id`.
7. Colour by trust: **Properties -> Symbology -> Categorized**, Value =
   `verified`. Everything auto-generated is `false` - that is deliberate, and
   it is your to-do list.
8. What you should see:
   - **two rows**, one either side of the road, not one long strip;
   - the **south row** running `PROP-001` (east) to `PROP-010` (west);
   - the **north row** running `PROP-011` (west) to `PROP-016` (east);
   - a clear gap down the middle - no zone crosses the carriageway;
   - a small gap between neighbouring zones - none of them touch;
   - one surveyed entrance point sitting inside each zone.

Open the attribute table (`F6`) and confirm 16 rows with
`source = FIELD_SURVEY_PLUS_AUTO_GEOMETRY`.

**Only correct what is actually wrong.** The zones are a first approximation
derived from the surveyed entrance points, not cadastral truth. Adjust a zone
when it clearly disagrees with the basemap - it covers the wrong gate, or is
much wider or narrower than the real plot frontage. Where a zone looks
reasonable, leave it. As you check each one, set `verified = true` in the
attribute table so you can tell checked from unchecked at a glance. The engine
works either way; `verified` is provenance, not a switch.

Quick check that an edit reached the database:

```bash
psql -d wastraq_demo -c \
  "SELECT zone_id, property_id, verified, round(ST_Area(geometry::geography)) AS m2
     FROM property_service_zones ORDER BY zone_id;"
```

## 6. What the real lane looks like in the database

| Column | Value for the auto-generated geometry |
|---|---|
| `property_service_zones.source` | `FIELD_SURVEY_PLUS_AUTO_GEOMETRY` |
| `property_frontages.source` | `FIELD_SURVEY_PLUS_AUTO_GEOMETRY` |
| `property_entrances.source` | `FIELD_SURVEY` (these are the surveyed points, untouched) |
| `verified` on zones and frontages | `false` - provisional, awaiting your eye |
| `verified` on entrances | `true` - collected on site |
| `properties.verification_status` | `FIELD_SURVEYED` |

Reload the lane at any time with `./scripts/load_real_lane.sh`; it backs the
current tables up to `logs/` first and never drops the database.

## 7. Replacing the lane geometry wholesale

When you're ready to map an actual street:

```bash
psql -d wastraq_demo <<'SQL'
DELETE FROM property_service_zones;
DELETE FROM property_frontages;
DELETE FROM property_entrances;
DELETE FROM evidence;
DELETE FROM collection_events;
DELETE FROM properties;
SQL
```

Then add the real properties and draw the real geometry over an OSM or satellite
basemap using the workflow in §4. Re-point `simulation/simulate_picker.py`'s
`TRACK` at coordinates on the real lane and the same demo runs unchanged.

Keep `scripts/generate_gis_data.py` around — it regenerates the synthetic lane
any time you want to reset to a known-good state.

## 8. Handy validation queries

```sql
-- properties missing any part of their GIS structure
SELECT p.property_id
FROM properties p
LEFT JOIN property_entrances     e ON e.property_id = p.property_id
LEFT JOIN property_frontages     f ON f.property_id = p.property_id
LEFT JOIN property_service_zones z ON z.property_id = p.property_id
WHERE e.entrance_id IS NULL OR f.frontage_id IS NULL OR z.zone_id IS NULL;

-- invalid or overlapping polygons (both break association)
SELECT zone_id, ST_IsValidReason(geometry) FROM property_service_zones WHERE NOT ST_IsValid(geometry);

SELECT a.zone_id, b.zone_id, round(ST_Area(ST_Intersection(a.geometry, b.geometry)::geography)) AS overlap_m2
FROM property_service_zones a JOIN property_service_zones b ON a.zone_id < b.zone_id
WHERE ST_Overlaps(a.geometry, b.geometry);

-- entrance that isn't inside its own service zone (usually a mis-click)
SELECT e.entrance_id, e.property_id
FROM property_entrances e JOIN property_service_zones z USING (property_id)
WHERE NOT ST_Within(e.geometry, z.geometry);

-- zone areas in real metres
SELECT zone_id, round(ST_Area(geometry::geography)) AS m2 FROM property_service_zones ORDER BY zone_id;
```

## 9. Seeing collection events on the map

**Layer → Add Layer → Add/Edit Virtual Layer**, or add a PostGIS layer from a
query. Quickest route: DB Manager (**Database → DB Manager → PostGIS →
wastraq_demo → SQL window**), run:

```sql
SELECT z.zone_id, z.geometry, c.event_id, c.segregation_status, c.collection_time
FROM property_service_zones z
LEFT JOIN LATERAL (
  SELECT * FROM collection_events ce
  WHERE ce.property_id = z.property_id
  ORDER BY ce.collection_time DESC LIMIT 1
) c ON TRUE;
```

Tick **Load as new layer**, set *Geometry column* = `geometry`, *Unique id* =
`zone_id`, then style it **Categorized** on `segregation_status` — green for
`SEGREGATED`, red for `NOT_SEGREGATED`. That's the dashboard map, in QGIS.
