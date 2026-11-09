import secrets
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .traccar_client import TraccarClient, TraccarError, commit_progress

MANAGER = 'elblasy_fleet_gps.group_gps_manager'
POLL_BACKOFF = 30          # seconds added to the wait after a failed on-demand poll
POLL_MIN_SECONDS = 8      # on-demand polling never asks Traccar more often than this
PUSH_FRESH_SECONDS = 90   # a push this recent means Traccar delivers by itself
GAP_FILL_MAX_H = 6
GAPFILL_EVERY = timedelta(minutes=5)


class GpsServer(models.Model):
    _name = 'fleet.gps.server'
    _description = "Traccar Server"
    _order = 'name'

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company, index=True)
    base_url = fields.Char("Traccar URL", required=True, help="e.g. https://traccar.example.com")
    # Secrets are only readable by GPS managers (field-level groups).
    api_token = fields.Char("API Token", groups=MANAGER, copy=False,
                            help="Traccar user token (Account > Token). Preferred over email/password.")
    user = fields.Char("Email", groups=MANAGER, copy=False)
    password = fields.Char(groups=MANAGER, copy=False)
    verify_ssl = fields.Boolean("Verify HTTPS Certificate", default=True)
    timeout = fields.Integer("Timeout (s)", default=30)
    push_token = fields.Char("Push Token", groups=MANAGER, copy=False, index=True,
                             default=lambda s: secrets.token_urlsafe(24))
    push_url = fields.Char("Push URL (forward.url)", compute='_compute_push_url', groups=MANAGER)
    event_push_url = fields.Char("Event Push URL (event.forward.url)", compute='_compute_push_url', groups=MANAGER)
    use_polling = fields.Boolean("Polling Safety Net", default=True,
                                 help="A cron fetches the latest positions to cover gaps when the push is not delivered.")
    last_test_date = fields.Datetime(readonly=True)
    last_test_result = fields.Char(readonly=True)
    last_push_date = fields.Datetime(readonly=True)
    last_poll_date = fields.Datetime(readonly=True)
    last_gapfill_date = fields.Datetime(readonly=True)
    last_event_time = fields.Datetime(readonly=True)
    device_ids = fields.One2many('fleet.gps.device', 'server_id')
    geofence_ids = fields.One2many('fleet.gps.geofence', 'server_id')
    device_count = fields.Integer(compute='_compute_device_count')

    _sql_constraints = [('push_token_uniq', 'unique(push_token)', "The push token must be unique.")]

    @api.constrains('base_url')
    def _check_base_url(self):
        for rec in self:
            if not (rec.base_url or '').lower().startswith(('http://', 'https://')):
                raise UserError(_("The Traccar URL must start with http:// or https://."))

    def _compute_device_count(self):
        data = dict(self.env['fleet.gps.device']._read_group(
            [('server_id', 'in', self.ids)], ['server_id'], ['__count']))
        for rec in self:
            rec.device_count = data.get(rec, 0)

    @api.depends('push_token')
    def _compute_push_url(self):
        base = (self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')
        for rec in self:
            token = rec.sudo().push_token
            rec.push_url = f"{base}/fleet_gps/push/{token}" if token else False
            rec.event_push_url = f"{base}/fleet_gps/event/{token}" if token else False

    def _get_client(self):
        self.ensure_one()
        s = self.sudo()
        # interactive callers (an open map, a playback) pass a short connect timeout: a dead Traccar must not hold a worker for long
        connect = self.env.context.get('traccar_connect_timeout') or 5
        return TraccarClient(s.base_url, token=s.api_token, user=s.user, password=s.password,
                             timeout=(connect, s.timeout), verify_ssl=s.verify_ssl)

    def _call(self, path, params=None):
        try:
            return self._get_client().get(path, params)
        except TraccarError as e:
            raise UserError(_("Traccar: %s", str(e)))

    def _put(self, path, payload):
        try:
            return self._get_client().put(path, payload)
        except TraccarError as e:
            raise UserError(_("Traccar: %s", str(e)))

    # ----- actions -------------------------------------------------------------------------------------------------
    def action_test_connection(self):
        self.ensure_one()
        try:
            info = self._get_client().get('server')
            result = _("OK — Traccar %(v)s", v=info.get('version', '?'))
            ok = True
        except TraccarError as e:
            result, ok = str(e), False
        self.sudo().write({'last_test_date': fields.Datetime.now(), 'last_test_result': result})
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _("Traccar"), 'message': result, 'type': 'success' if ok else 'danger', 'sticky': not ok}}

    def action_regenerate_push_token(self):
        for rec in self:
            rec.sudo().push_token = secrets.token_urlsafe(24)

    def action_import_devices(self):
        self.ensure_one()
        n_new, n_upd = self.env['fleet.gps.device']._import_from_server(self)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _("Devices imported"),
            'message': _("%(new)s new, %(upd)s updated.", new=n_new, upd=n_upd), 'type': 'success'}}

    def action_view_devices(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _("Devices"), 'res_model': 'fleet.gps.device',
                'view_mode': 'tree,form', 'domain': [('server_id', '=', self.id)],
                'context': {'default_server_id': self.id}}

    # ----- polling (safety net and "no push" mode) -----------------------------------------------------------------------
    def _poll(self, fill_gaps=False):
        """Pull the latest position of every device. ``fill_gaps`` also fetches what each device reported since the
        last position we hold, so the track (trips, distance) stays complete while no push reaches Odoo."""
        self.ensure_one()
        Position = self.env['fleet.gps.position'].with_context(from_poll=True)
        devices = {d['id']: d for d in self._call('devices')}
        self.env['fleet.gps.device']._apply_device_status(self, list(devices.values()))
        latest = self._call('positions')
        if fill_gaps:
            known = {d.traccar_id: d for d in self.env['fleet.gps.device'].search([('server_id', '=', self.id)])}
            now = fields.Datetime.now()
            for pos in list(latest):
                dev = known.get(pos.get('deviceId'))
                if not dev or not dev.last_fix_time or pos.get('id', 0) <= dev.last_position_id + 1:
                    continue
                start = max(dev.last_fix_time - timedelta(minutes=1), now - timedelta(hours=GAP_FILL_MAX_H))
                try:
                    latest += self._call('positions', {'deviceId': dev.traccar_id, 'from': start.isoformat() + 'Z',
                                                       'to': now.isoformat() + 'Z'})
                except UserError:
                    continue  # the latest position is enough for the map; the gap stays
        Position._ingest(self, latest, devices)
        if fill_gaps:
            self._poll_events(list(devices))
        self.sudo().last_poll_date = fields.Datetime.now()

    def _poll_events(self, traccar_device_ids):
        """Events since the last one we hold (Traccar keeps them; a push may have missed some)."""
        self.ensure_one()
        if not traccar_device_ids:
            return
        now = fields.Datetime.now()
        start = (self.last_event_time or now - timedelta(hours=1)) - timedelta(minutes=1)
        try:
            events = self._call('reports/events', {'deviceId': traccar_device_ids, 'type': 'allEvents',
                                                   'from': start.isoformat() + 'Z', 'to': now.isoformat() + 'Z'})
        except UserError:
            return
        self.env['fleet.gps.event']._ingest(self, events)

    def _post(self, path, payload):
        try:
            return self._get_client().post(path, payload)
        except TraccarError as e:
            raise UserError(_("Traccar: %s", str(e)))

    @api.model
    def poll_now(self):
        """Called by an open live map when no push reaches Odoo. Throttled per server and single-flight (a row lock),
        so ten open maps still cost one request to Traccar per interval."""
        if not self.env.user.has_group('elblasy_fleet_gps.group_gps_user'):
            raise AccessError(_("You are not allowed to use the GPS tracking."))
        done = 0
        for server in self.sudo().search([('use_polling', '=', True)]):
            server.flush_recordset(['last_poll_date'])  # the raw query below reads the database
            self.env.cr.execute("""
                SELECT id FROM fleet_gps_server WHERE id = %s AND (last_poll_date IS NULL
                    OR last_poll_date < (now() AT TIME ZONE 'UTC') - make_interval(secs => %s))
                FOR UPDATE SKIP LOCKED""", (server.id, POLL_MIN_SECONDS))
            if not self.env.cr.fetchone():
                continue
            try:
                server.with_context(traccar_connect_timeout=2)._poll()
                done += 1
            except UserError:
                # do not hammer a server that is down: the next on-demand try is POLL_BACKOFF later
                server.last_poll_date = fields.Datetime.now() + timedelta(seconds=POLL_BACKOFF)
        return done

    @api.model
    def _push_active(self):
        """True when some server pushed positions recently: maps then wait for the bus instead of polling."""
        limit = fields.Datetime.now() - timedelta(seconds=PUSH_FRESH_SECONDS)
        return bool(self.sudo().search_count([('last_push_date', '>', limit)]))

    # ----- crons ---------------------------------------------------------------------------------------------------
    @api.model
    def _cron_poll(self):
        """Runs every minute. Without push it keeps statuses and positions fresh by itself (two requests a minute); with
        push it only checks in every GAPFILL_EVERY as a safety net. The per-device gap fill is the costly part, so it
        runs at most every GAPFILL_EVERY either way."""
        now = fields.Datetime.now()
        push_limit = now - timedelta(seconds=PUSH_FRESH_SECONDS)
        for server in self.search([('use_polling', '=', True)]):
            pushing = bool(server.last_push_date and server.last_push_date > push_limit)
            if pushing and server.last_poll_date and now - server.last_poll_date < GAPFILL_EVERY:
                continue
            fill = not server.last_gapfill_date or now - server.last_gapfill_date >= GAPFILL_EVERY
            try:
                server._poll(fill_gaps=fill)
                if fill:
                    server.sudo().last_gapfill_date = now
            except UserError:
                continue  # a down Traccar must not stop the other servers
            commit_progress(self.env)
