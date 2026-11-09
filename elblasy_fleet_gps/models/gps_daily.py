import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from odoo import api, fields, models

from . import analytics
from .traccar_client import commit_progress

_logger = logging.getLogger(__name__)
DEFAULT_TZ = 'Africa/Cairo'
MERGE_M = 200  # stops closer than this are the same place
FINAL_AFTER = timedelta(hours=14)  # devices may back-fill late; after this a finished day is not recomputed again


def _ms_to_dt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None, microsecond=0)


class GpsDaily(models.Model):
    _name = 'fleet.gps.daily'
    _description = "GPS Daily Summary"
    _order = 'date desc, device_id'

    device_id = fields.Many2one('fleet.gps.device', required=True, ondelete='cascade', index=True)
    vehicle_id = fields.Many2one('fleet.vehicle', related='device_id.vehicle_id', store=True, index=True)
    driver_id = fields.Many2one('res.partner', string="Driver", index=True)
    company_id = fields.Many2one(related='device_id.company_id', store=True, index=True)
    date = fields.Date(required=True, index=True)
    distance_km = fields.Float("Distance (km)", digits=(12, 2), group_operator='sum')
    moving_hours = fields.Float("Driving (h)", digits=(8, 2), group_operator='sum')
    idle_hours = fields.Float("Idling (h)", digits=(8, 2), group_operator='sum')
    stopped_hours = fields.Float("Stopped (h)", digits=(8, 2), group_operator='sum')
    max_speed = fields.Float("Max Speed (km/h)", digits=(6, 1), group_operator='max')
    avg_speed = fields.Float("Avg Speed (km/h)", digits=(6, 1), group_operator='avg')
    trip_count = fields.Integer("Trips", group_operator='sum')
    over_count = fields.Integer("Overspeed Events", group_operator='sum')
    over_hours = fields.Float("Overspeed (h)", digits=(8, 2), group_operator='sum')
    harsh_brake = fields.Integer("Harsh Braking", group_operator='sum')
    harsh_accel = fields.Integer("Harsh Acceleration", group_operator='sum')
    harsh_total = fields.Integer("Harsh Events", compute='_compute_harsh', store=True, group_operator='sum')
    score = fields.Float("Driving Score", digits=(5, 0), group_operator='avg')
    points = fields.Integer("GPS Points", group_operator='sum')
    speed_limit = fields.Integer("Limit Used (km/h)")
    hour_dist = fields.Json("Distance per hour (m)")
    hour_moving = fields.Json("Moving per hour (ms)")
    speed_hist = fields.Json("Time per speed band (ms)")
    final = fields.Boolean(help="The day is over and settled: it is not recomputed.")
    trip_ids = fields.One2many('fleet.gps.trip', 'daily_id')
    stop_ids = fields.One2many('fleet.gps.stop', 'daily_id')

    _sql_constraints = [('device_date_uniq', 'unique(device_id, date)', "One summary per device and day.")]

    @api.depends('harsh_brake', 'harsh_accel')
    def _compute_harsh(self):
        for rec in self:
            rec.harsh_total = rec.harsh_brake + rec.harsh_accel

    # ----- computation -----------------------------------------------------------------------------------------------
    @api.model
    def _tz(self):
        name = (self.env['ir.config_parameter'].sudo().get_param('elblasy_fleet_gps.timezone') or '') or DEFAULT_TZ
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - unknown zone name in settings
            return ZoneInfo(DEFAULT_TZ)

    @api.model
    def _day_bounds(self, day, tz):
        """UTC naive [start, end) of the local calendar day (23 or 25 h long around DST)."""
        start = datetime(day.year, day.month, day.day, tzinfo=tz)
        end = datetime(*(day + timedelta(days=1)).timetuple()[:3], tzinfo=tz)
        return start.astimezone(timezone.utc).replace(tzinfo=None), end.astimezone(timezone.utc).replace(tzinfo=None)

    @api.model
    def _compute_for(self, device, day, force=False):
        """(Re)build the summary, trips and stops of one device and local day from its stored positions."""
        tz = self._tz()
        existing = self.search([('device_id', '=', device.id), ('date', '=', day)])
        if existing.final and not force:
            return existing
        start, end = self._day_bounds(day, tz)
        self.env.cr.execute("""
            SELECT (extract(epoch FROM fix_time) * 1000)::bigint, latitude, longitude, speed, ignition
              FROM fleet_gps_position WHERE device_id = %s AND fix_time >= %s AND fix_time < %s
             ORDER BY fix_time, id""", (device.id, start, end))
        rows = self.env.cr.fetchall()
        if not rows and not existing:
            return existing
        limit = int(self.env['ir.config_parameter'].sudo().get_param('elblasy_fleet_gps.speed_limit') or 0) or 80
        hours = {}

        def hour_of(ms):
            k = ms // 3600000
            if k not in hours:
                hours[k] = datetime.fromtimestamp(k * 3600, tz=timezone.utc).astimezone(tz).hour
            return hours[k]

        g = analytics.compute(rows, limit, hour_of)
        moving_ms = g['moving_ms']
        harsh = g['harsh_brake'] + g['harsh_accel']
        final = fields.Datetime.now() - end > FINAL_AFTER
        vals = {
            'device_id': device.id, 'date': day, 'driver_id': device.vehicle_id.driver_id.id or False,
            'distance_km': g['dist'] / 1000, 'moving_hours': moving_ms / 3.6e6, 'idle_hours': g['idle_ms'] / 3.6e6,
            'stopped_hours': g['stop_ms'] / 3.6e6, 'max_speed': g['max_speed'],
            'avg_speed': g['speed_sum'] / moving_ms if moving_ms else 0.0, 'trip_count': g['trips'],
            'over_count': g['over_count'], 'over_hours': g['over_ms'] / 3.6e6,
            'harsh_brake': g['harsh_brake'], 'harsh_accel': g['harsh_accel'],
            'score': analytics.driver_score(g['dist'], moving_ms, g['over_ms'], harsh) if moving_ms else 100,
            'points': g['points'], 'speed_limit': limit, 'hour_dist': [round(x) for x in g['hour_dist']],
            'hour_moving': g['hour_moving'], 'speed_hist': g['hist'], 'final': final,
        }
        if existing:
            existing.write(vals)
            rec = existing
        else:
            rec = self.create(vals)
        rec.trip_ids.unlink()
        rec.stop_ids.unlink()
        self.env['fleet.gps.trip'].create([{
            'daily_id': rec.id, 'start_time': _ms_to_dt(t['s']), 'end_time': _ms_to_dt(t['e']),
            'start_lat': t['s_lat'], 'start_lon': t['s_lon'], 'end_lat': t['e_lat'], 'end_lon': t['e_lon'],
            'distance_km': t['dist'] / 1000, 'max_speed': t['max'], 'avg_speed': t['avg'],
            'moving_minutes': t['mov'] / 60000} for t in g['trip_list']])
        fences = self.env['fleet.gps.geofence'].search([('server_id', '=', device.server_id.id)])
        Stop = self.env['fleet.gps.stop']
        known = []
        Stop.create([{
            'daily_id': rec.id, 'start_time': _ms_to_dt(s['s']), 'end_time': _ms_to_dt(s['e']),
            'latitude': s['lat'], 'longitude': s['lon'], 'ignition': s['ign'], **Stop._place_vals(s['lat'], s['lon'], fences, known)}
            for s in g['stop_list']])
        return rec

    # ----- crons -----------------------------------------------------------------------------------------------------
    @api.model
    def _cron_compute(self):
        """Keep today and yesterday current for every device that has positions there."""
        tz = self._tz()
        today = datetime.now(timezone.utc).astimezone(tz).date()
        devices = self.env['fleet.gps.device'].search([('last_position_id', '>', 0)])
        for device in devices:
            for day in (today - timedelta(days=1), today):
                self._compute_for(device, day)
            commit_progress(self.env)


class GpsTrip(models.Model):
    _name = 'fleet.gps.trip'
    _description = "GPS Trip"
    _order = 'start_time desc'

    daily_id = fields.Many2one('fleet.gps.daily', required=True, ondelete='cascade', index=True)
    device_id = fields.Many2one(related='daily_id.device_id', store=True, index=True)
    vehicle_id = fields.Many2one(related='daily_id.vehicle_id', store=True, index=True)
    driver_id = fields.Many2one(related='daily_id.driver_id', store=True)
    company_id = fields.Many2one(related='daily_id.company_id', store=True, index=True)
    date = fields.Date(related='daily_id.date', store=True, index=True)
    start_time = fields.Datetime(required=True)
    end_time = fields.Datetime(required=True)
    duration_minutes = fields.Float("Duration (min)", compute='_compute_duration', store=True, digits=(8, 1), group_operator='sum')
    moving_minutes = fields.Float("Moving (min)", digits=(8, 1), group_operator='sum')
    distance_km = fields.Float("Distance (km)", digits=(10, 2), group_operator='sum')
    max_speed = fields.Float("Max Speed (km/h)", digits=(6, 1), group_operator='max')
    avg_speed = fields.Float("Avg Speed (km/h)", digits=(6, 1), group_operator='avg')
    start_lat = fields.Float(digits=(10, 7))
    start_lon = fields.Float(digits=(10, 7))
    end_lat = fields.Float(digits=(10, 7))
    end_lon = fields.Float(digits=(10, 7))

    def action_playback(self):
        trip = self[:1]
        pad = timedelta(minutes=2)
        return trip.device_id.action_playback(trip.start_time - pad, trip.end_time + pad)

    @api.depends('start_time', 'end_time')
    def _compute_duration(self):
        for t in self:
            t.duration_minutes = (t.end_time - t.start_time).total_seconds() / 60 if t.start_time and t.end_time else 0


class GpsStop(models.Model):
    _name = 'fleet.gps.stop'
    _description = "GPS Stop"
    _order = 'start_time desc'

    daily_id = fields.Many2one('fleet.gps.daily', required=True, ondelete='cascade', index=True)
    device_id = fields.Many2one(related='daily_id.device_id', store=True, index=True)
    vehicle_id = fields.Many2one(related='daily_id.vehicle_id', store=True, index=True)
    company_id = fields.Many2one(related='daily_id.company_id', store=True, index=True)
    date = fields.Date(related='daily_id.date', store=True, index=True)
    start_time = fields.Datetime(required=True)
    end_time = fields.Datetime(required=True)
    duration_minutes = fields.Float("Duration (min)", compute='_compute_duration', store=True, digits=(8, 1), group_operator='sum')
    latitude = fields.Float(digits=(10, 7))
    longitude = fields.Float(digits=(10, 7))
    ignition = fields.Boolean("Engine On", help="The engine was running while standing (idling).")
    geofence_id = fields.Many2one('fleet.gps.geofence', string="Geofence", ondelete='set null', index=True)
    place = fields.Char(help="The geofence the stop is inside, else its position rounded to about 100 m: stops at the same "
                             "yard, customer or queue group together.", index=True)

    @api.model
    def _place_vals(self, lat, lon, fences, known=None):
        """The geofence the point is inside, else the place of a stop within MERGE_M of it (a loading yard, a queue, a
        customer site is one place however wide it is), else a new place named by its rounded position."""
        for fence in fences:
            if fence._contains(lat, lon):
                return {'geofence_id': fence.id, 'place': fence.name}
        known = known if known is not None else []
        for k_lat, k_lon, name in known:
            if abs(k_lat - lat) < 0.003 and abs(k_lon - lon) < 0.003 and analytics.haversine(k_lat, k_lon, lat, lon) <= MERGE_M:
                return {'geofence_id': False, 'place': name}
        self.env.cr.execute("""
            SELECT place, latitude, longitude FROM fleet_gps_stop
             WHERE geofence_id IS NULL AND place IS NOT NULL AND latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s
             ORDER BY id LIMIT 20""", (lat - 0.003, lat + 0.003, lon - 0.003, lon + 0.003))
        for name, k_lat, k_lon in self.env.cr.fetchall():
            if analytics.haversine(k_lat, k_lon, lat, lon) <= MERGE_M:
                known.append((k_lat, k_lon, name))
                return {'geofence_id': False, 'place': name}
        name = f"{lat:.3f}, {lon:.3f}"
        known.append((lat, lon, name))
        return {'geofence_id': False, 'place': name}

    @api.model
    def _assign_places(self):
        """Fill ``place`` on stops created before it existed (upgrade) or after the geofences changed."""
        for stops in self.search([('place', '=', False)]).grouped('company_id').values():
            fences = self.env['fleet.gps.geofence'].search([('company_id', '=', stops[0].company_id.id)])
            known = []
            for s in stops.sorted('start_time'):
                s.write(self._place_vals(s.latitude, s.longitude, fences, known))

    @api.depends('start_time', 'end_time')
    def _compute_duration(self):
        for s in self:
            s.duration_minutes = (s.end_time - s.start_time).total_seconds() / 60 if s.start_time and s.end_time else 0
