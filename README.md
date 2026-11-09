# Elblasy Fleet GPS (Traccar) for Odoo 17

Live tracking, trips, reports and route playback for Odoo Fleet vehicles, powered by your own [Traccar](https://www.traccar.org) server.

- Live map (MapLibre), dashboard with insights, daily summary / trips / stops / waiting / behaviour / compare reports (Odoo pivot, graph, list), events, geofences, route playback.
- Every GPS device is linked to a `fleet.vehicle`. Devices, geofences and history are imported from Traccar.
- Push (`forward.url`) is optional: without it an open map pulls every 10 s and a cron keeps statuses fresh.
- Odoo 17 Community or Enterprise. Depends on `fleet`, `bus`, `mail`. English and Arabic (RTL), dark mode, phone / tablet / desktop.

Install: copy the `elblasy_fleet_gps` folder to your addons path (or push this repository to Odoo.sh), update the apps list and install **Elblasy Fleet GPS (Traccar)**.
See `elblasy_fleet_gps/static/description/index.html` for the full description.

License: LGPL-3. Third-party: MapLibre GL JS (BSD-3-Clause), mapbox-gl-rtl-text (BSD-2-Clause), see `elblasy_fleet_gps/static/lib/maplibre/`.
