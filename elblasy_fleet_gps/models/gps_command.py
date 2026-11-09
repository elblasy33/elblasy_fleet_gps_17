from odoo import _, api, fields, models
from odoo.exceptions import UserError

COMMANDS = [
    ('custom', "Custom (raw text)"), ('positionPeriodic', "Report position every N seconds"), ('positionSingle', "Report position now"),
    ('rebootDevice', "Reboot device"), ('alarmArm', "Arm alarm"), ('alarmDisarm', "Disarm alarm"),
    ('engineStop', "Stop engine (cut fuel/power)"), ('engineResume', "Resume engine"),
]
DANGEROUS = {'engineStop'}


class GpsCommand(models.TransientModel):
    _name = 'fleet.gps.command'
    _description = "Send a Command to a GPS Device"

    device_id = fields.Many2one('fleet.gps.device', required=True, readonly=True)
    command_type = fields.Selection(COMMANDS, required=True, default='positionSingle')
    data = fields.Char("Command text", help="Only for a custom command: sent to the device as is.")
    frequency = fields.Integer("Every (seconds)", default=30)
    confirm_stop = fields.Boolean("I understand this can stop a moving vehicle")
    dangerous = fields.Boolean(compute='_compute_dangerous')

    @api.depends('command_type')
    def _compute_dangerous(self):
        for w in self:
            w.dangerous = w.command_type in DANGEROUS

    def _payload(self):
        self.ensure_one()
        attrs = {}
        if self.command_type == 'custom':
            if not (self.data or '').strip():
                raise UserError(_("Type the command text to send."))
            attrs['data'] = self.data.strip()
        elif self.command_type == 'positionPeriodic':
            if not 5 <= self.frequency <= 86400:
                raise UserError(_("The interval must be between 5 and 86400 seconds."))
            attrs['frequency'] = self.frequency
        return {'deviceId': self.device_id.traccar_id, 'type': self.command_type, 'attributes': attrs, 'textChannel': False}

    def action_send(self):
        self.ensure_one()
        if self.dangerous and not self.confirm_stop:
            raise UserError(_("Tick the confirmation: stopping an engine while the vehicle is moving is dangerous."))
        payload = self._payload()
        self.device_id.server_id._post('commands/send', payload)
        self.device_id.message_post(body=_("Command sent to the device by %(user)s: %(cmd)s", user=self.env.user.name,
                                                    cmd=dict(COMMANDS)[self.command_type]))
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _("Command sent"), 'message': dict(COMMANDS)[self.command_type], 'type': 'success'}}
