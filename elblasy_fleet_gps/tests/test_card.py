from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .test_gps import Common, _pos

POST = 'odoo.addons.elblasy_fleet_gps.models.traccar_client.TraccarClient.post'
HTTP = 'odoo.addons.elblasy_fleet_gps.models.gps_geocache.requests.get'


def _ts(minutes_ago):
    return (fields.Datetime.now() - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%dT%H:%M:%S.000+00:00')


def _ev(eid, etype, minutes_ago, **attrs):
    return {'id': eid, 'type': etype, 'deviceId': 1, 'eventTime': _ts(minutes_ago), 'attributes': attrs}


@tagged('post_install', '-at_install')
class TestCardExtra(Common):

    def setUp(self):
        super().setUp()
        self.dev = self.env['fleet.gps.device'].create({'name': 'C', 'server_id': self.server.id, 'traccar_id': 1, 'status': 'online'})
        self.Dev = self.env['fleet.gps.device']

    def _positions(self, rows):
        out = []
        for i, (mins, lat, lon, kmh, attrs) in enumerate(rows, 1):
            p = _pos(i, fix=_ts(mins), srv=_ts(mins), speed=kmh / 1.852, **attrs)
            p['latitude'], p['longitude'] = lat, lon
            out.append(p)
        self.env['fleet.gps.position']._ingest(self.server, out, {})

    def test_state_since_comes_from_events_that_agree_with_the_state(self):
        self._positions([(1, 30.0, 31.0, 0, {'ignition': True})])
        self.env['fleet.gps.event']._ingest(self.server, [_ev(1, 'ignitionOn', 125), _ev(2, 'deviceStopped', 95), _ev(3, 'deviceMoving', 300)])
        x = self.Dev.get_card_extra(self.dev.id)
        self.assertAlmostEqual(x['since_ignition'] / 60000, (fields.Datetime.now().timestamp() - 125 * 60) / 60, delta=3)
        self.assertIsNotNone(x['since_motion'])  # stopped now and the newest motion event is deviceStopped
        self.env['fleet.gps.event']._ingest(self.server, [_ev(4, 'ignitionOff', 10)])  # now disagrees with "ignition on"
        self.assertIsNone(self.Dev.get_card_extra(self.dev.id)['since_ignition'])

    def test_still_since_is_the_run_inside_150_metres(self):
        self._positions([(60, 30.0000, 31.0, 0, {}), (50, 30.0003, 31.0, 0, {}), (40, 30.0, 31.0002, 0, {}),
                         (30, 30.0, 31.0, 0, {}), (200, 31.0, 31.0, 50, {})])  # a point far away, hours earlier
        x = self.Dev.get_card_extra(self.dev.id)['still']
        self.assertAlmostEqual((fields.Datetime.now().timestamp() * 1000 - x['since']) / 60000, 60, delta=2)
        self.assertFalse(x['at_least'])

    def test_a_single_point_proves_no_stay(self):
        self._positions([(5, 30.0, 31.0, 0, {})])
        self.assertIsNone(self.Dev.get_card_extra(self.dev.id)['still'])

    def test_silence_cause(self):
        cause = self.Dev._silence_cause
        self.assertEqual(cause([{'alarm': 'powerCut'}, {}]), 'power')
        self.assertEqual(cause([{'charge': True, 'batteryLevel': 80}]), 'network')       # wired power, then silence
        self.assertEqual(cause([{'charge': False, 'batteryLevel': 10}]), 'power')        # empty battery
        self.assertEqual(cause([{'charge': False, 'batteryLevel': 80}]), 'unknown')      # on battery only: not provable
        self.assertEqual(cause([{'batteryLevel': 70}]), 'network')
        self.assertEqual(cause([{}]), 'unknown')
        self.assertEqual(cause([]), 'unknown')

    def test_card_reports_why_an_offline_vehicle_is_quiet(self):
        self._positions([(120, 30.0, 31.0, 0, {'charge': True, 'batteryLevel': 90})])
        self.dev.write({'status': 'offline', 'last_fix_time': fields.Datetime.now() - timedelta(minutes=120)})
        self.assertEqual(self.Dev.get_card_extra(self.dev.id)['silence'], 'network')
        self.dev.status = 'online'
        self.dev.last_fix_time = fields.Datetime.now()
        self.assertIsNone(self.Dev.get_card_extra(self.dev.id)['silence'])

    def test_latest_events_and_access(self):
        self.env['fleet.gps.event']._ingest(self.server, [_ev(i, 'alarm', i, alarm='sos') for i in range(1, 8)])
        x = self.Dev.get_card_extra(self.dev.id)
        self.assertEqual(len(x['events']), 5)
        self.assertIn('(sos)', x['events'][0]['label'])
        outsider = self.env['res.users'].with_context(no_reset_password=True).create({'name': 'N5', 'login': 'nogps5', 'email': 'n5@example.com'})
        with self.assertRaises(AccessError):
            self.Dev.with_user(outsider).get_card_extra(self.dev.id)
        with self.assertRaises(UserError):
            self.Dev.get_card_extra(999999)

    def test_live_payload_carries_the_registration_data(self):
        self.dev.write({'unique_id': '359', 'phone': '0110', 'iccid': '8920', 'model': 'VT05S', 'group_name': 'Trucks'})
        d = self.Dev.get_live_data()['devices'][0]
        self.assertEqual((d['imei'], d['phone'], d['iccid'], d['model'], d['group']), ('359', '0110', '8920', 'VT05S', 'Trucks'))


@tagged('post_install', '-at_install')
class TestGeocoding(Common):

    def setUp(self):
        super().setUp()
        self.Cache = self.env['fleet.gps.geocache']
        self.reply = MagicMock()
        self.reply.json.return_value = {'address': {'road': 'Ring Road', 'suburb': 'Maadi', 'city': 'Cairo'}}

    def test_off_by_default_and_sends_nothing(self):
        with patch(HTTP) as get:
            self.assertFalse(self.Cache._lookup(30.0, 31.0))
        get.assert_not_called()

    def test_lookup_builds_a_short_address_and_caches_it_per_cell(self):
        self.env['ir.config_parameter'].set_param('elblasy_fleet_gps.geocoding', 'True')
        self.env.user.lang = 'en_US'
        with patch(HTTP, return_value=self.reply) as get:
            self.assertEqual(self.Cache._lookup(30.0001, 31.0001), 'Ring Road, Maadi, Cairo')
            self.assertEqual(self.Cache._lookup(30.0004, 31.0002), 'Ring Road, Maadi, Cairo')  # same ~100 m cell
        self.assertEqual(get.call_count, 1)
        self.assertIn('Odoo', get.call_args.kwargs['headers']['User-Agent'])  # Nominatim's policy: identify the app
        self.assertEqual(get.call_args.kwargs['params']['accept-language'], 'en')

    def test_failure_and_missing_position_return_false(self):
        self.env['ir.config_parameter'].set_param('elblasy_fleet_gps.geocoding', 'True')
        with patch(HTTP, side_effect=requests.ConnectionError('down')):
            self.assertFalse(self.Cache._lookup(30.0, 31.0))
        with patch(HTTP) as get:
            self.assertFalse(self.Cache._lookup(0, 0))
        get.assert_not_called()
        self.assertEqual(self.Cache.search_count([]), 0)  # a failure is not cached


@tagged('post_install', '-at_install')
class TestCommands(Common):

    def setUp(self):
        super().setUp()
        self.dev = self.env['fleet.gps.device'].create({'name': 'K', 'server_id': self.server.id, 'traccar_id': 7})
        self.Wiz = self.env['fleet.gps.command']

    def _send(self, **vals):
        wiz = self.Wiz.create({'device_id': self.dev.id, **vals})
        with patch(POST, return_value={}) as post:
            wiz.action_send()
        return post.call_args.args

    def test_payloads(self):
        path, payload = self._send(command_type='positionSingle')
        self.assertEqual((path, payload['deviceId'], payload['type'], payload['attributes']), ('commands/send', 7, 'positionSingle', {}))
        self.assertEqual(self._send(command_type='positionPeriodic', frequency=60)[1]['attributes'], {'frequency': 60})
        self.assertEqual(self._send(command_type='custom', data=' SMS,1 ')[1]['attributes'], {'data': 'SMS,1'})

    def test_validation_and_the_dangerous_one_needs_confirmation(self):
        for vals in ({'command_type': 'custom', 'data': ' '}, {'command_type': 'positionPeriodic', 'frequency': 1}):
            with self.assertRaises(UserError):
                self.Wiz.create({'device_id': self.dev.id, **vals}).action_send()
        with self.assertRaises(UserError), patch(POST) as post:
            self.Wiz.create({'device_id': self.dev.id, 'command_type': 'engineStop'}).action_send()
        post.assert_not_called()
        self.assertEqual(self._send(command_type='engineStop', confirm_stop=True)[1]['type'], 'engineStop')

    def test_sending_is_logged_on_the_device_and_traccar_errors_surface(self):
        self._send(command_type='rebootDevice')
        self.assertTrue(any('Command sent' in str(m.body) for m in self.dev.message_ids))
        from odoo.addons.elblasy_fleet_gps.models.traccar_client import TraccarError
        with self.assertRaises(UserError), patch(POST, side_effect=TraccarError('http', 'HTTP 400')):
            self.Wiz.create({'device_id': self.dev.id, 'command_type': 'rebootDevice'}).action_send()

    def test_only_managers_can_send(self):
        user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'GU2', 'login': 'gu2', 'email': 'gu2@example.com',
            'groups_id': [(6, 0, [self.env.ref('elblasy_fleet_gps.group_gps_user').id])]})
        with self.assertRaises(AccessError):
            self.Wiz.with_user(user).create({'device_id': self.dev.id, 'command_type': 'rebootDevice'})
