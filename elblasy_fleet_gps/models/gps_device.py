import re

import math
import time
from datetime import timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import analytics
from .traccar_client import TraccarError, parse_time as _parse_time

SYNCED = {'name', 'category', 'color'}  # edited in Odoo, written back to Traccar like the Tracar dashboard did


def _date(value):
    dt = _parse_time(value)
    if dt:
        return dt.date()
    try:  # Traccar attributes can hold a plain date (2026-10-18)
        return fields.Date.to_date(value[:10]) if isinstance(value, str) and value else False
    except (ValueError, TypeError):
        return False

BUS_TYPE = 'elblasy_fleet_gps/live'
BUS_CHANNEL = 'elblasy_fleet_gps'
ICON_DEFAULT, ICON_MIN, ICON_MAX = 140, 50, 250  # percent: the Tracar dashboard drew its icons at 1.4x
SUBSCRIPTION_WARN_DAYS = 14
LIVE_FRESH = timedelta(minutes=5)
STAY_RADIUS_M = 150          # moving about inside a circle 300 m across is still the same place
POWER_ALARMS = re.compile(r'powerCut|lowBattery|powerOff', re.I)
LOW_BATTERY = 20
NOTIFY_EVERY = 1.5  # seconds: a push per position would flood the bus table
_last_notify = {}

STATUS = [('online', "Online"), ('offline', "Offline"), ('unknown', "Unknown")]


def _norm(text):
    """Plate / name key: case-insensitive, without spaces and punctuation."""
    return re.sub(r'[\W_]+', '', (text or '').casefold())


class GpsDevice(models.Model):
    _name = 'fleet.gps.device'
    _description = "GPS Device"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'name'

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    server_id = fields.Many2one('fleet.gps.server', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='server_id.company_id', store=True, index=True)
    traccar_id = fields.Integer("Traccar ID", required=True, index=True, copy=False)
    unique_id = fields.Char("IMEI / Unique ID", copy=False)
    phone = fields.Char("SIM Number")
    iccid = fields.Char("ICCID")
    model = fields.Char("Device Model")
    group_name = fields.Char("Traccar Group")
    category = fields.Char("Icon Category", default='default', help="Traccar category (car, truck, container, bus ...).")
    color = fields.Char("Map Color")
    disabled = fields.Boolean("Disabled in Traccar")
    subscription_start = fields.Date("Subscription Start", help="Traccar attribute activatedAt.")
    subscription_end = fields.Date("Subscription End", index=True, help="Traccar attribute expiresAt.")
    live_state = fields.Selection(
        [('moving', "Moving"), ('over', "Over speed"), ('idle', "Idling"), ('stopped', "Stopped"), ('offline', "Offline"), ('nofix', "No position")],
        compute='_compute_live_state', help="The state shown on the map: Traccar's online/offline plus the last speed and ignition.")
    last_seen_ago = fields.Char("Last Fix Age", compute='_compute_live_state')
    expiry_notified = fields.Date(help="Subscription end date an alert was already created for.", copy=False)
    subscription_state = fields.Selection(
        [('none', "No subscription"), ('ok', "Active"), ('soon', "Expiring soon"), ('expired', "Expired")],
        compute='_compute_subscription_state')
    vehicle_id = fields.Many2one('fleet.vehicle', string="Vehicle", index=True, tracking=True, ondelete='set null',
                                 check_company=True)
    driver_id = fields.Many2one(related='vehicle_id.driver_id')

    status = fields.Selection(STATUS, default='unknown', index=True)
    last_update = fields.Datetime("Last Seen")
    last_position_id = fields.Integer("Last Traccar Position ID")
    last_fix_time = fields.Datetime("Last Fix")
    last_latitude = fields.Float(digits=(10, 7))
    last_longitude = fields.Float(digits=(10, 7))
    last_speed = fields.Float("Speed (km/h)", digits=(8, 1))
    last_course = fields.Float(digits=(5, 1))
    last_ignition = fields.Boolean("Ignition")
    last_battery = fields.Float("Battery (%)", digits=(5, 1))
    last_address = fields.Char("Address")
    position_ids = fields.One2many('fleet.gps.position', 'device_id')

    @api.depends('status', 'last_fix_time', 'last_speed', 'last_ignition', 'last_latitude', 'last_longitude')
    def _compute_live_state(self):
        """Same rule as the live map (vehicle.js): an old fix is not "moving now", whatever speed it carried."""
        now = fields.Datetime.now()
        limit = int(self.env['ir.config_parameter'].sudo().get_param('elblasy_fleet_gps.speed_limit') or 0) or 80
        for d in self:
            if d.status != 'online':
                state = 'offline'
            elif not d.last_fix_time or (not d.last_latitude and not d.last_longitude):
                state = 'nofix'
            elif now - d.last_fix_time < LIVE_FRESH and d.last_speed > 2:
                state = 'over' if d.last_speed > limit else 'moving'
            else:
                state = 'idle' if d.last_ignition else 'stopped'
            d.live_state = state
            if d.last_fix_time:
                minutes = int((now - d.last_fix_time).total_seconds() // 60)
                d.last_seen_ago = (_("just now") if minutes < 1 else
                                   f"{minutes} {_('min')}" if minutes < 120 else f"{minutes // 60} {_('h')}")
            else:
                d.last_seen_ago = False

    @api.depends('subscription_end')
    def _compute_subscription_state(self):
        today = fields.Date.context_today(self)
        for rec in self:
            end = rec.subscription_end
            rec.subscription_state = 'none' if not end else 'expired' if end < today else \
                'soon' if (end - today).days <= 14 else 'ok'

    _sql_constraints = [('server_traccar_uniq', 'unique(server_id, traccar_id)', "This Traccar device already exists.")]

    def init(self):
        super().init()
        # a vehicle can only have one active GPS device
        self.env.cr.execute("""CREATE UNIQUE INDEX IF NOT EXISTS fleet_gps_device_vehicle_uniq
                               ON fleet_gps_device (vehicle_id) WHERE vehicle_id IS NOT NULL AND active IS TRUE""")

    # ----- import --------------------------------------------------------------------------------------------------
    @api.model
    def _import_from_server(self, server):
        groups = {g['id']: g['name'] for g in server._call('groups')}
        raw = server._call('devices')
        existing = {d.traccar_id: d for d in self.with_context(active_test=False).search([('server_id', '=', server.id)])}
        n_new = n_upd = 0
        for dev in raw:
            attrs = dev.get('attributes') or {}
            vals = {
                'name': dev.get('name') or dev.get('uniqueId') or str(dev['id']),
                'unique_id': dev.get('uniqueId'),
                'phone': dev.get('phone') or False,
                'model': dev.get('model') or False,
                'group_name': groups.get(dev.get('groupId')) or False,
                'category': dev.get('category') or 'default',
                'color': attrs.get('tracarColor') or False,
                'iccid': (attrs.get('iccid') or '').strip() or False,
                'disabled': bool(dev.get('disabled')),
                'subscription_start': _date(attrs.get('activatedAt')),
                'subscription_end': _date(attrs.get('expiresAt')),
            }
            rec = existing.get(dev['id'])
            if rec:
                rec.with_context(no_traccar_sync=True).write(vals)
                n_upd += 1
            else:
                rec = self.with_context(no_traccar_sync=True).create({**vals, 'server_id': server.id, 'traccar_id': dev['id']})
                n_new += 1
                existing[dev['id']] = rec
        self.env['fleet.gps.geofence']._import_from_server(server)
        self._apply_device_status(server, raw)
        self.search([('server_id', '=', server.id), ('vehicle_id', '=', False)])._auto_link_vehicle()
        return n_new, n_upd

    @api.model
    def _apply_device_status(self, server, raw_devices):
        devs = {d.traccar_id: d for d in self.search([('server_id', '=', server.id)])}
        for raw in raw_devices:
            rec = devs.get(raw.get('id'))
            if not rec:
                continue
            status = raw.get('status') if raw.get('status') in ('online', 'offline') else 'unknown'
            vals = {}
            if rec.status != status:
                vals['status'] = status
            last = _parse_time(raw.get('lastUpdate'))
            if last and last != rec.last_update:
                vals['last_update'] = last
            if vals:
                rec.write(vals)
        self._notify_live()

    # ----- vehicle link ----------------------------------------------------------------------------------------------
    def _auto_link_vehicle(self):
        """Link by normalised plate or vehicle name, only when the match is unambiguous and the vehicle is free."""
        vehicles = self.env['fleet.vehicle'].search([('company_id', 'in', self.company_id.ids)])
        taken = set(self.search([('vehicle_id', '!=', False)]).vehicle_id.ids)
        index = {}
        for v in vehicles:
            for key in {_norm(v.license_plate), _norm(v.name)} - {''}:
                index.setdefault((v.company_id.id, key), set()).add(v.id)
        linked = 0
        for dev in self.filtered(lambda d: not d.vehicle_id):
            hits = index.get((dev.company_id.id, _norm(dev.name)), set()) - taken
            if len(hits) == 1:
                vid = hits.pop()
                dev.vehicle_id = vid
                taken.add(vid)
                linked += 1
        return linked

    def action_auto_link(self):
        self._auto_link_vehicle()

    def action_create_vehicle(self):
        """Create a Fleet vehicle for devices that have none (needs a model: the first one is used)."""
        model = self.env['fleet.vehicle.model'].search([], limit=1)
        for dev in self.filtered(lambda d: not d.vehicle_id):
            if not model:
                model = self.env['fleet.vehicle.model'].create({
                    'name': 'Generic', 'brand_id': self.env['fleet.vehicle.model.brand'].create({'name': 'Generic'}).id})
            dev.vehicle_id = self.env['fleet.vehicle'].create({
                'model_id': model.id, 'license_plate': dev.name, 'company_id': dev.company_id.id}).id

    def action_view_vehicle(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'fleet.vehicle', 'res_id': self.vehicle_id.id,
                'view_mode': 'form'}


    # ----- live map ----------------------------------------------------------------------------------------------------
    @api.model
    def _notify_live(self):
        """Tell open maps that something changed. The message carries no data (the map reads through RPC, with the
        user's own access rights) and is coalesced: at most one per NOTIFY_EVERY seconds per worker and database."""
        now = time.monotonic()
        db = self.env.cr.dbname
        if now - _last_notify.get(db, 0) < NOTIFY_EVERY:
            return
        _last_notify[db] = now
        # the message carries no data, so a plain channel the open maps subscribe to is enough
        self.env['bus.bus'].sudo()._sendone(BUS_CHANNEL, BUS_TYPE, {})

    def _live_vals(self):
        self.ensure_one()
        v = self.vehicle_id
        return {
            'id': self.id,
            'name': self.name,
            'plate': v.license_plate or False,
            'vehicle_id': v.id or False,
            'vehicle': v.display_name if v else False,
            'driver': self.driver_id.name or False,
            'category': self.category or 'default',
            'color': self.color or False,
            'status': self.status,
            'last_update': fields.Datetime.to_string(self.last_update) if self.last_update else False,
            'fix_time': fields.Datetime.to_string(self.last_fix_time) if self.last_fix_time else False,
            'lat': self.last_latitude,
            'lon': self.last_longitude,
            'speed': self.last_speed,
            'course': self.last_course,
            'ignition': self.last_ignition,
            'battery': self.last_battery or False,
            'address': self.last_address or False,
            'imei': self.unique_id or False,
            'phone': self.phone or False,
            'iccid': self.iccid or False,
            'model': self.model or False,
            'group': self.group_name or False,
            'subscription_end': fields.Date.to_string(self.subscription_end) if self.subscription_end else False,
            'subscription_state': self.subscription_state,
        }

    @api.model
    def _icon_size(self):
        """Vehicle icon size in percent (setting), kept within a sane range."""
        value = int(self.env['ir.config_parameter'].sudo().get_param('elblasy_fleet_gps.icon_size') or 0) or ICON_DEFAULT
        return max(ICON_MIN, min(ICON_MAX, value))

    @api.model
    def get_live_data(self, since=None):
        """Everything the live map needs. With ``since`` (the ``now`` of a previous answer) only the devices written
        since are returned, so a refresh costs a few rows instead of the whole fleet."""
        params = self.env['ir.config_parameter'].sudo()
        domain = [('disabled', '=', False)]
        if since:
            domain.append(('write_date', '>=', fields.Datetime.to_datetime(since)))
        self.env.cr.execute("SELECT now() AT TIME ZONE 'UTC'")
        now = self.env.cr.fetchone()[0]
        devices = self.search(domain)
        return {
            'now': fields.Datetime.to_string(now - timedelta(seconds=2)),  # overlap: a row committed late is not missed
            'full': not since,
            'devices': [d._live_vals() for d in devices],
            'config': {
                'push_active': self.env['fleet.gps.server']._push_active(),
                'speed_limit': int(params.get_param('elblasy_fleet_gps.speed_limit') or 0) or 80,
                'icon_size': self._icon_size(),
                'colors': {
                    'moving': (params.get_param('elblasy_fleet_gps.color_moving') or '') or '#23e76a',
                    'idle': (params.get_param('elblasy_fleet_gps.color_idle') or '') or '#f59e0b',
                    'stopped': (params.get_param('elblasy_fleet_gps.color_stopped') or '') or '#27aef1',
                    'over': (params.get_param('elblasy_fleet_gps.color_over') or '') or '#f20202',
                    'offline': (params.get_param('elblasy_fleet_gps.color_offline') or '') or '#727374',
                },
            },
        }

    # ----- write back to Traccar -----------------------------------------------------------------------------------------
    def write(self, vals):
        res = super().write(vals)
        if SYNCED & set(vals) and not self.env.context.get('no_traccar_sync'):
            self._push_to_traccar()
        return res

    def _push_to_traccar(self):
        """Name / icon category / map colour of the device in Traccar. PUT needs the whole device object, so it is read
        first and only these fields are changed. A failure never blocks the edit: it is noted on the device."""
        for dev in self:
            try:
                remote = dev.server_id._call(f'devices/{dev.traccar_id}')
                remote['name'] = dev.name
                remote['category'] = dev.category or 'default'
                attrs = dict(remote.get('attributes') or {})
                if dev.color:
                    attrs['tracarColor'] = dev.color
                else:
                    attrs.pop('tracarColor', None)
                remote['attributes'] = attrs
                dev.server_id._put(f'devices/{dev.traccar_id}', remote)
            except Exception as e:  # noqa: BLE001 - UserError, or anything odd in the payload
                dev.message_post(body=_("Could not update Traccar: %s", str(e)))

    # ----- subscription alerts -------------------------------------------------------------------------------------------
    @api.model
    def _cron_subscription_alerts(self):
        """One to-do per device and subscription end date, 14 days before it (or when it already passed)."""
        today = fields.Date.context_today(self)
        limit = today + timedelta(days=SUBSCRIPTION_WARN_DAYS)
        todo = self.search([('subscription_end', '!=', False), ('subscription_end', '<=', limit)])
        for dev in todo.filtered(lambda d: d.expiry_notified != d.subscription_end):
            user = dev.vehicle_id.manager_id or self.env['res.users'].search(
                [('groups_id', 'in', self.env.ref('elblasy_fleet_gps.group_gps_manager').ids), ('share', '=', False)], limit=1)
            summary = (_("GPS subscription expired: %s", dev.name) if dev.subscription_end < today
                       else _("GPS subscription ends on %(date)s: %(name)s", date=dev.subscription_end, name=dev.name))
            dev.activity_schedule('mail.mail_activity_data_todo', date_deadline=dev.subscription_end if dev.subscription_end >= today else today,
                                  summary=summary, user_id=(user or self.env.user).id)
            dev.expiry_notified = dev.subscription_end

    def action_playback(self, start=None, end=None):
        self.ensure_one()
        return {'type': 'ir.actions.client', 'tag': 'elblasy_fleet_gps_playback', 'name': _("Playback"),
                'params': {'device_id': self.id, 'start': start and fields.Datetime.to_string(start), 'end': end and fields.Datetime.to_string(end)}}

    # ----- vehicle card: what the live payload leaves out ------------------------------------------------------------------
    @api.model
    def get_card_extra(self, device_id):
        """For the selected vehicle only (one device, a few queries): since when it has been in its current engine / motion
        state, since when it has been at this place, why it went quiet, its latest events and a street address."""
        dev = self.browse(device_id).exists()
        if not dev:
            raise UserError(_("Unknown device."))
        dev.check_access_rights('read')
        dev.check_access_rule('read')
        cr = self.env.cr
        now = fields.Datetime.now()
        moving = dev.live_state in ('moving', 'over')

        def ms(dt):
            return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000) if dt else None

        # Traccar's own transition events say when the state began. Only the newest event of a pair counts, and only when it
        # agrees with the state reported now: otherwise the events cannot vouch for it.
        cr.execute("""SELECT DISTINCT ON (kind) kind, type, event_time FROM (
                          SELECT CASE WHEN type IN ('ignitionOn', 'ignitionOff') THEN 'ign' ELSE 'motion' END AS kind, type, event_time
                            FROM fleet_gps_event WHERE device_id = %s AND type IN ('ignitionOn', 'ignitionOff', 'deviceMoving', 'deviceStopped')
                      ) e ORDER BY kind, event_time DESC""", (dev.id,))
        since = {}
        for kind, etype, when in cr.fetchall():
            want = ('ignitionOn' if dev.last_ignition else 'ignitionOff') if kind == 'ign' else ('deviceMoving' if moving else 'deviceStopped')
            since[kind] = ms(when) if etype == want and when <= now + timedelta(minutes=2) else None

        # at this place since: the unbroken run of positions within STAY_RADIUS_M of the last one
        cr.execute("""SELECT fix_time, latitude, longitude FROM fleet_gps_position WHERE device_id = %s AND fix_time > %s
                         AND NOT (latitude = 0 AND longitude = 0) ORDER BY fix_time DESC LIMIT 8000""", (dev.id, now - timedelta(hours=24)))
        rows = cr.fetchall()
        still = None
        if len(rows) > 1:
            lat0, lon0 = rows[0][1], rows[0][2]
            oldest, n = rows[0][0], 0
            for when, lat, lon in rows[1:]:
                if analytics.haversine(lat0, lon0, lat, lon) > STAY_RADIUS_M:
                    break
                oldest, n = when, n + 1
            still = {'since': ms(oldest), 'at_least': n == len(rows) - 1} if n else None

        silence = None
        if dev.status != 'online' or (dev.last_fix_time and now - dev.last_fix_time > timedelta(minutes=10)):
            cr.execute("""SELECT attributes FROM fleet_gps_position WHERE device_id = %s ORDER BY fix_time DESC, id DESC LIMIT 12""", (dev.id,))
            silence = self._silence_cause([r[0] or {} for r in cr.fetchall()])

        events = self.env['fleet.gps.event'].search_read([('device_id', '=', dev.id)], ['type', 'event_time', 'alarm'], limit=5)
        label = dict(self.env['fleet.gps.event']._fields['type']._description_selection(self.env))
        return {
            'since_ignition': since.get('ign'), 'since_motion': since.get('motion'), 'still': still, 'silence': silence,
            'events': [{'label': label.get(e['type'], e['type']) + (f" ({e['alarm']})" if e['alarm'] else ''),
                        'time': fields.Datetime.to_string(e['event_time'])} for e in events],
            'address': dev.last_address or self.env['fleet.gps.geocache']._lookup(dev.last_latitude, dev.last_longitude),
        }

    @api.model
    def _silence_cause(self, attrs_rows):
        """Why a tracker went quiet, judged from its last messages (newest first): 'power' (cut, empty battery), 'network'
        (healthy power, then silence: a SIM or coverage problem) or 'unknown' (not provable). Traccar cannot see the SIM plan."""
        recent = attrs_rows[:12]
        if any(isinstance(a.get('alarm'), str) and POWER_ALARMS.search(a['alarm']) for a in recent):
            return 'power'
        charge = next((a['charge'] for a in recent if isinstance(a.get('charge'), bool)), None)
        battery = next((a['batteryLevel'] for a in recent if isinstance(a.get('batteryLevel'), (int, float)) and not isinstance(a.get('batteryLevel'), bool)), None)
        if charge is True:
            return 'network'
        if charge is False:
            return 'power' if battery is None or battery <= LOW_BATTERY else 'unknown'
        return 'network' if battery is not None and battery > LOW_BATTERY else 'unknown'

    def action_open_command(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'fleet.gps.command', 'view_mode': 'form', 'target': 'new',
                'context': {'default_device_id': self.id}}
