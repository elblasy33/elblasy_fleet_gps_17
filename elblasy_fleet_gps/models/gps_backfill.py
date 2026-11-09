import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from .traccar_client import commit_progress

_logger = logging.getLogger(__name__)
MAX_DAYS = 92
STEPS_PER_RUN = 12  # (device, day) pairs one cron run handles: each is one Traccar request


class GpsBackfill(models.Model):
    _name = 'fleet.gps.backfill'
    _description = "GPS History Import"
    _order = 'id desc'

    name = fields.Char(compute='_compute_name', store=True)
    server_id = fields.Many2one('fleet.gps.server', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='server_id.company_id', store=True)
    date_from = fields.Date(required=True, default=lambda s: fields.Date.context_today(s) - timedelta(days=7))
    date_to = fields.Date(required=True, default=fields.Date.context_today)
    device_ids = fields.Many2many('fleet.gps.device', string="Devices", domain="[('server_id', '=', server_id)]",
                                  help="Leave empty for all devices of the server.")
    state = fields.Selection([('draft', "Draft"), ('running', "Running"), ('done', "Done"), ('failed', "Failed")],
                             default='draft', required=True)
    cursor = fields.Integer(help="Index of the next (device, day) pair.")
    total = fields.Integer(readonly=True)
    progress = fields.Float(compute='_compute_progress')
    positions = fields.Integer("Positions Imported", readonly=True)
    error = fields.Text(readonly=True)

    @api.depends('server_id', 'date_from', 'date_to')
    def _compute_name(self):
        for rec in self:
            rec.name = f"{rec.server_id.name or ''} {rec.date_from or ''} → {rec.date_to or ''}"

    @api.depends('cursor', 'total')
    def _compute_progress(self):
        for rec in self:
            rec.progress = 100.0 * rec.cursor / rec.total if rec.total else 0.0

    def _devices(self):
        self.ensure_one()
        return self.device_ids or self.env['fleet.gps.device'].search([('server_id', '=', self.server_id.id)])

    def _days(self):
        self.ensure_one()
        return (self.date_to - self.date_from).days + 1

    def action_start(self):
        for rec in self:
            days = rec._days()
            if days < 1 or days > MAX_DAYS:
                raise UserError(_("Choose a range of 1 to %s days.", MAX_DAYS))
            rec.write({'state': 'running', 'cursor': 0, 'total': days * len(rec._devices()), 'error': False, 'positions': 0})

    def action_run_now(self):
        """One batch right away (the cron does the same every minute)."""
        self._step()

    def _step(self, steps=STEPS_PER_RUN):
        Daily = self.env['fleet.gps.daily']
        tz = Daily._tz()
        for rec in self.filtered(lambda r: r.state == 'running'):
            devices, days = rec._devices(), rec._days()
            try:
                for __ in range(steps):
                    if rec.cursor >= rec.total:
                        break
                    device, day = devices[rec.cursor // days], rec.date_from + timedelta(days=rec.cursor % days)
                    start, end = Daily._day_bounds(day, tz)
                    # a day ending in the future is partial: Traccar just answers with what it has
                    route = rec.server_id._call('reports/route', {
                        'deviceId': device.traccar_id, 'from': start.isoformat() + 'Z', 'to': end.isoformat() + 'Z'})
                    stored = self.env['fleet.gps.position'].with_context(no_live_notify=True)._ingest(rec.server_id, route, {})
                    Daily._compute_for(device, day, force=True)
                    rec.write({'cursor': rec.cursor + 1, 'positions': rec.positions + stored})
                if rec.cursor >= rec.total:
                    rec.state = 'done'
            except UserError as e:
                rec.write({'state': 'failed', 'error': str(e)})
            commit_progress(self.env)

    @api.model
    def _cron_step(self):
        self.search([('state', '=', 'running')])._step()
