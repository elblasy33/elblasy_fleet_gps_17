import logging
from datetime import timedelta, timezone

from psycopg2.extras import Json, execute_values

from odoo import _, api, fields, models, tools
from odoo.exceptions import UserError

from .traccar_client import parse_time, commit_progress

_logger = logging.getLogger(__name__)

KNOT_KMH = 1.852
MAX_WINDOW = timedelta(hours=49)   # a playback covers a day or a trip, never a month
MAX_POINTS = 15000                 # more is decimated: the browser draws them all
AHEAD = timedelta(minutes=2)  # a fix this far after its server time is a clock error, never a "latest"
PURGE_BATCH = 50000


class GpsPosition(models.Model):
    _name = 'fleet.gps.position'
    _description = "GPS Position"
    _order = 'fix_time desc, id desc'
    _log_access = False  # millions of rows: no create/write uid and date columns

    device_id = fields.Many2one('fleet.gps.device', required=True, ondelete='cascade', index=True)
    vehicle_id = fields.Many2one(related='device_id.vehicle_id', store=False)
    company_id = fields.Many2one('res.company', required=True, index=True)
    traccar_id = fields.Integer("Traccar Position ID", required=True)
    fix_time = fields.Datetime(required=True)
    server_time = fields.Datetime()
    latitude = fields.Float(digits=(10, 7))
    longitude = fields.Float(digits=(10, 7))
    altitude = fields.Float(digits=(8, 1))
    speed = fields.Float("Speed (km/h)", digits=(8, 1))
    course = fields.Float(digits=(5, 1))
    valid = fields.Boolean()
    ignition = fields.Boolean()
    motion = fields.Boolean()
    battery = fields.Float("Battery (%)", digits=(5, 1))
    odometer = fields.Float("Total Distance (m)")
    address = fields.Char()
    attributes = fields.Json()

    _sql_constraints = [('device_position_uniq', 'unique(device_id, traccar_id)', "Duplicate position.")]

    def init(self):
        super().init()
        tools.create_index(self.env.cr, 'fleet_gps_position_device_fix_idx', self._table, ['device_id', 'fix_time'])

    # ----- ingestion -------------------------------------------------------------------------------------------------
    @api.model
    def _ingest(self, server, positions, devices_info=None):
        """Store Traccar position dicts. ``devices_info`` maps Traccar device id -> Traccar device dict
        (used to create devices we have not imported yet). Returns the number of new rows."""
        positions = [p for p in positions or [] if isinstance(p, dict) and p.get('deviceId') and p.get('id')]
        if not positions:
            return 0
        Device = self.env['fleet.gps.device'].sudo()
        devices = {d.traccar_id: d for d in Device.search([('server_id', '=', server.id)])}
        for p in positions:
            did = p['deviceId']
            if did not in devices:
                info = (devices_info or {}).get(did) or {}
                devices[did] = Device.create({
                    'server_id': server.id, 'traccar_id': did,
                    'name': info.get('name') or info.get('uniqueId') or f"Device {did}",
                    'unique_id': info.get('uniqueId'), 'category': info.get('category') or 'default'})
        rows, latest = [], {}
        for p in positions:
            fix, srv = parse_time(p.get('fixTime')), parse_time(p.get('serverTime'))
            if not fix:
                continue
            dev = devices[p['deviceId']]
            a = p.get('attributes') or {}
            speed = (p.get('speed') or 0) * KNOT_KMH
            batt = a.get('batteryLevel', a.get('battery'))
            rows.append((
                dev.id, server.company_id.id, p['id'], fix, srv, p.get('latitude') or 0, p.get('longitude') or 0,
                p.get('altitude') or 0, speed, p.get('course') or 0, bool(p.get('valid')),
                bool(a.get('ignition')), bool(a.get('motion')),
                batt if isinstance(batt, (int, float)) and not isinstance(batt, bool) and batt <= 100 else None,
                a.get('totalDistance') if isinstance(a.get('totalDistance'), (int, float)) else None,
                p.get('address') or None, Json(a)))
            ahead = bool(srv and fix - srv > AHEAD)
            cur = latest.get(dev.id)
            if not ahead and (not cur or p['id'] > cur[0]['id']):
                latest[dev.id] = (p, fix, speed, batt)
        inserted = 0
        if rows:
            # a fixed order: two writers (push, poll, history import) inserting overlapping rows in different orders can deadlock
            rows.sort(key=lambda r: (r[0], r[2]))
            done = execute_values(self.env.cr._obj, """
                INSERT INTO fleet_gps_position (device_id, company_id, traccar_id, fix_time, server_time, latitude,
                    longitude, altitude, speed, course, valid, ignition, motion, battery, odometer, address, attributes)
                VALUES %s ON CONFLICT (device_id, traccar_id) DO NOTHING RETURNING id""", rows, fetch=True)
            inserted = len(done)
        by_id = {d.id: d for d in devices.values()}
        for dev_id, (p, fix, speed, batt) in latest.items():
            dev = by_id[dev_id]
            if p['id'] <= dev.last_position_id:
                continue
            a = p.get('attributes') or {}
            dev.with_context(tracking_disable=True).write({
                'last_position_id': p['id'], 'last_fix_time': fix, 'last_latitude': p.get('latitude') or 0,
                'last_longitude': p.get('longitude') or 0, 'last_speed': speed, 'last_course': p.get('course') or 0,
                'last_ignition': bool(a.get('ignition')),
                'last_battery': batt if isinstance(batt, (int, float)) and not isinstance(batt, bool) and batt <= 100 else dev.last_battery,
                'last_address': p.get('address') or dev.last_address})
        if inserted and not self.env.context.get('no_live_notify'):  # a history import is not live traffic
            if not self.env.context.get('from_poll'):  # only a real push proves Traccar can reach Odoo
                server.sudo().write({'last_push_date': fields.Datetime.now()})
            Device._notify_live()
        return inserted

    # ----- retention ---------------------------------------------------------------------------------------------------
    @api.model
    def _cron_purge(self):
        self.env['fleet.gps.event']._cron_purge()
        days = int(self.env['ir.config_parameter'].sudo().get_param('elblasy_fleet_gps.retention_days') or 0) or 30
        cutoff = fields.Datetime.now() - timedelta(days=days)
        while True:
            self.env.cr.execute("""DELETE FROM fleet_gps_position WHERE id IN (
                SELECT id FROM fleet_gps_position WHERE fix_time < %s LIMIT %s)""", (cutoff, PURGE_BATCH))
            if self.env.cr.rowcount < PURGE_BATCH:
                break
            if not commit_progress(self.env, self.env.cr.rowcount):
                break

    # ----- playback ----------------------------------------------------------------------------------------------------
    @api.model
    def get_route(self, device_id, start=None, end=None, day=None):
        """One device's route for the playback screen, in compact form: ``pts`` rows are
        ``[ms, lat, lon, km/h, course, ignition]``. ``day`` (yyyy-mm-dd) is a local day of the reports timezone, else
        ``start`` / ``end`` are UTC datetimes. Read from the stored positions; when there are none (a day older than
        the retention, or never received) it is fetched from Traccar on the fly and not stored."""
        self.check_access_rights('read')
        device = self.env['fleet.gps.device'].browse(device_id).exists()
        if not device:
            raise UserError(_("Unknown device."))
        device.check_access_rights('read')
        device.check_access_rule('read')  # the raw query below does not apply record rules: this does
        if day:
            daily = self.env['fleet.gps.daily']
            d_start, d_end = daily._day_bounds(fields.Date.to_date(day), daily._tz())
        else:
            d_start, d_end = fields.Datetime.to_datetime(start), fields.Datetime.to_datetime(end)
        if not d_start or not d_end or d_end <= d_start or d_end - d_start > MAX_WINDOW:
            raise UserError(_("Choose a window of at most 48 hours."))
        self.env.cr.execute("""
            SELECT (extract(epoch FROM fix_time) * 1000)::bigint, latitude, longitude, speed, course, ignition
              FROM fleet_gps_position
             WHERE device_id = %s AND fix_time >= %s AND fix_time < %s AND NOT (latitude = 0 AND longitude = 0)
             ORDER BY fix_time, id""", (device.id, d_start, d_end))
        pts = [[ms, lat, lon, round(kmh, 1), round(course), 1 if ign else 0] for ms, lat, lon, kmh, course, ign in self.env.cr.fetchall()]
        source = 'stored'
        if not pts:
            source = 'traccar'
            try:
                remote = device.server_id.with_context(traccar_connect_timeout=3)._call('reports/route', {'deviceId': device.traccar_id, 'from': d_start.isoformat() + 'Z',
                                                                  'to': d_end.isoformat() + 'Z'})
            except UserError:
                remote = []
            for p in remote:
                when = parse_time(p.get('fixTime'))
                if when and (p.get('latitude') or p.get('longitude')):
                    ms = int(when.replace(tzinfo=timezone.utc).timestamp() * 1000)
                    pts.append([ms, p['latitude'], p['longitude'], round((p.get('speed') or 0) * KNOT_KMH, 1),
                                round(p.get('course') or 0), 1 if (p.get('attributes') or {}).get('ignition') else 0])
            pts.sort()
        if len(pts) > MAX_POINTS:
            step = -(-len(pts) // MAX_POINTS)
            pts = pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else [])
        stops = self.env['fleet.gps.stop'].search_read(
            [('device_id', '=', device.id), ('end_time', '>', d_start), ('start_time', '<', d_end)],
            ['start_time', 'end_time', 'latitude', 'longitude', 'ignition', 'place', 'duration_minutes'], order='start_time')
        trips = self.env['fleet.gps.trip'].search_read(
            [('device_id', '=', device.id), ('end_time', '>', d_start), ('start_time', '<', d_end)],
            ['start_time', 'end_time', 'distance_km', 'max_speed'], order='start_time')
        return {
            'device': {'id': device.id, 'name': device.name, 'plate': device.vehicle_id.license_plate or False,
                       'category': device.category or 'default', 'color': device.color or False},
            'window': [fields.Datetime.to_string(d_start), fields.Datetime.to_string(d_end)],
            'pts': pts, 'source': source, 'truncated': len(pts) >= MAX_POINTS,
            'limit': int(self.env['ir.config_parameter'].sudo().get_param('elblasy_fleet_gps.speed_limit') or 0) or 80,
            'icon_size': self.env['fleet.gps.device']._icon_size(),
            'stops': [{'start': fields.Datetime.to_string(s['start_time']), 'end': fields.Datetime.to_string(s['end_time']),
                       'lat': s['latitude'], 'lon': s['longitude'], 'ign': s['ignition'], 'place': s['place'] or '',
                       'minutes': round(s['duration_minutes'])} for s in stops],
            'trips': [{'start': fields.Datetime.to_string(t['start_time']), 'end': fields.Datetime.to_string(t['end_time']),
                       'km': round(t['distance_km'], 1), 'max': round(t['max_speed'])} for t in trips],
        }
