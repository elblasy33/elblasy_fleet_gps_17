import logging
from datetime import timedelta

from odoo import _, api, fields, models

from .traccar_client import parse_time

_logger = logging.getLogger(__name__)

EVENT_TYPES = [
    ('deviceOnline', "Device online"), ('deviceOffline', "Device offline"), ('deviceUnknown', "Device unknown"),
    ('deviceInactive', "Device inactive"), ('deviceMoving', "Started moving"), ('deviceStopped', "Stopped"),
    ('deviceOverspeed', "Overspeed"), ('deviceFuelDrop', "Fuel drop"), ('deviceFuelIncrease', "Fuel increase"),
    ('geofenceEnter', "Entered geofence"), ('geofenceExit', "Left geofence"), ('alarm', "Alarm"),
    ('ignitionOn', "Ignition on"), ('ignitionOff', "Ignition off"), ('maintenance', "Maintenance"),
    ('textMessage', "Text message"), ('driverChanged', "Driver changed"), ('commandResult', "Command result"),
    ('media', "Media"), ('other', "Other"),
]
KNOWN = {k for k, __ in EVENT_TYPES}
GROUPS = {  # search filters
    'connection': ('deviceOnline', 'deviceOffline', 'deviceUnknown', 'deviceInactive'),
    'motion': ('deviceMoving', 'deviceStopped'),
    'ignition': ('ignitionOn', 'ignitionOff'),
    'geofence': ('geofenceEnter', 'geofenceExit'),
}
PURGE_DAYS = 180
# alarms worth a chatter note; 'vibration' alone was 967 events a week from two parked trucks. At most one note per device and
# kind per NOTE_EVERY: a tracker repeating an alarm must not bury the real messages.
ALARM_NOTE = {'sos', 'powerCut', 'lowBattery', 'powerOff', 'tampering', 'accident'}
NOTE_EVERY = timedelta(hours=1)


class GpsEvent(models.Model):
    _name = 'fleet.gps.event'
    _description = "GPS Event"
    _order = 'event_time desc, id desc'
    _log_access = False

    device_id = fields.Many2one('fleet.gps.device', required=True, ondelete='cascade', index=True)
    vehicle_id = fields.Many2one('fleet.vehicle', related='device_id.vehicle_id', store=True, index=True)
    company_id = fields.Many2one('res.company', required=True, index=True)
    traccar_id = fields.Integer("Traccar Event ID", required=True)
    type = fields.Selection(EVENT_TYPES, required=True, index=True)
    type_raw = fields.Char("Traccar Type")
    event_time = fields.Datetime(required=True, index=True)
    geofence_id = fields.Many2one('fleet.gps.geofence', ondelete='set null')
    alarm = fields.Char(help="Traccar alarm kind (sos, powerCut, lowBattery ...).")
    latitude = fields.Float(digits=(10, 7))
    longitude = fields.Float(digits=(10, 7))
    attributes = fields.Json()
    seen = fields.Boolean(default=False, index=True)

    _sql_constraints = [('device_event_uniq', 'unique(device_id, traccar_id)', "Duplicate event.")]

    @api.model
    def _ingest(self, server, events, devices_info=None, positions=None, geofences=None):
        """Store Traccar event dicts. ``positions`` / ``geofences`` map Traccar id -> dict (from a push payload).
        Unknown devices are ignored: an event is not a reason to create one. Returns the number of new events."""
        events = [e for e in events or [] if isinstance(e, dict) and e.get('id') and e.get('deviceId') and e.get('type')]
        if not events:
            return 0
        devices = {d.traccar_id: d for d in self.env['fleet.gps.device'].sudo().search([('server_id', '=', server.id)])}
        fences = {g.traccar_id: g for g in self.env['fleet.gps.geofence'].sudo().with_context(active_test=False).search([('server_id', '=', server.id)])}
        existing = {(e.device_id.id, e.traccar_id) for e in self.sudo().search([
            ('device_id', 'in', [d.id for d in devices.values()]), ('traccar_id', 'in', [e['id'] for e in events])])}
        vals_list, alarms, last_note = [], [], {}
        for e in events:
            dev = devices.get(e['deviceId'])
            when = parse_time(e.get('eventTime'))
            if not dev or not when or (dev.id, e['id']) in existing:
                continue
            existing.add((dev.id, e['id']))
            attrs = e.get('attributes') or {}
            pos = (positions or {}).get(e.get('positionId')) or {}
            lat, lon = pos.get('latitude'), pos.get('longitude')
            if lat is None and e.get('positionId'):
                self.env.cr.execute("SELECT latitude, longitude FROM fleet_gps_position WHERE device_id = %s AND traccar_id = %s",
                                    (dev.id, e['positionId']))
                lat, lon = self.env.cr.fetchone() or (None, None)
            vals = {
                'device_id': dev.id, 'company_id': server.company_id.id, 'traccar_id': e['id'],
                'type': e['type'] if e['type'] in KNOWN else 'other', 'type_raw': e['type'], 'event_time': when,
                'geofence_id': fences[e['geofenceId']].id if e.get('geofenceId') in fences else False,
                'alarm': attrs.get('alarm') or False, 'latitude': lat or 0, 'longitude': lon or 0, 'attributes': attrs,
            }
            vals_list.append(vals)
            if vals['type'] == 'alarm' and vals['alarm'] in ALARM_NOTE and self._note_due(dev, vals['alarm'], when, last_note):
                alarms.append((dev, vals['alarm']))
        records = self.sudo().create(vals_list) if vals_list else self.browse()
        for dev, alarm in alarms:
            dev.message_post(body=_("Alarm: %s", alarm))
        if records:
            latest = max(records.mapped('event_time'))
            if not server.last_event_time or latest > server.last_event_time:
                server.sudo().last_event_time = latest
        return len(records)

    def _note_due(self, dev, alarm, when, last_note):
        key = (dev.id, alarm)
        prev = last_note.get(key)
        if prev is None:
            self.env.cr.execute("""SELECT max(event_time) FROM fleet_gps_event
                                   WHERE device_id = %s AND type = 'alarm' AND alarm = %s""", key)
            prev = self.env.cr.fetchone()[0]
        if prev and when - prev < NOTE_EVERY:
            last_note[key] = max(prev, when)
            return False
        last_note[key] = when
        return True

    @api.model
    def _cron_purge(self):
        cutoff = fields.Datetime.now() - timedelta(days=PURGE_DAYS)
        self.env.cr.execute("DELETE FROM fleet_gps_event WHERE event_time < %s", (cutoff,))

    def action_mark_seen(self):
        self.filtered(lambda e: not e.seen).write({'seen': True})

    @api.model
    def action_mark_all_seen(self):
        self.search([('seen', '=', False)]).write({'seen': True})
