from contextlib import nullcontext
import json
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.elblasy_fleet_gps.models.traccar_client import TraccarClient, TraccarError

CLIENT_GET = 'odoo.addons.elblasy_fleet_gps.models.traccar_client.TraccarClient.get'


def _pos(pid, dev=1, fix='2026-11-01T10:00:00.000+00:00', srv='2026-11-01T10:00:01.000+00:00', speed=10.0, **attrs):
    return {'id': pid, 'deviceId': dev, 'fixTime': fix, 'serverTime': srv, 'latitude': 30.0, 'longitude': 31.0,
            'speed': speed, 'course': 90, 'valid': True, 'attributes': attrs}


class Common(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = cls.env['fleet.gps.server'].create({'name': 'T', 'base_url': 'http://traccar.test', 'api_token': 'S3CRET'})
        brand = cls.env['fleet.vehicle.model.brand'].create({'name': 'B'})
        cls.model = cls.env['fleet.vehicle.model'].create({'name': 'M', 'brand_id': brand.id})
        cls.vehicle = cls.env['fleet.vehicle'].create({'model_id': cls.model.id, 'license_plate': 'ABC 123'})


@tagged('post_install', '-at_install')
class TestImportAndLink(Common):

    def _fake_get(self, path, params=None):
        return {
            'groups': [{'id': 7, 'name': 'Trucks'}],
            'geofences': [],
            'devices': [
                {'id': 1, 'name': 'ABC-123', 'uniqueId': '111', 'status': 'online', 'category': 'truck',
                 'groupId': 7, 'lastUpdate': '2026-11-01T10:00:00.000+00:00',
                 'attributes': {'tracarColor': '#ff0000', 'iccid': ' 8920 '}},
                {'id': 2, 'name': 'Other', 'uniqueId': '222', 'status': 'offline'},
            ],
        }[path]

    def test_import_is_idempotent_and_links_by_plate(self):
        with patch(CLIENT_GET, side_effect=self._fake_get):
            self.assertEqual(self.server.action_import_devices()['params']['message'][:7], '2 new, ')
            self.server.action_import_devices()
        devs = self.env['fleet.gps.device'].search([('server_id', '=', self.server.id)])
        self.assertEqual(len(devs), 2)
        d1 = devs.filtered(lambda d: d.traccar_id == 1)
        self.assertEqual(d1.vehicle_id, self.vehicle)  # "ABC-123" == "ABC 123"
        self.assertEqual((d1.group_name, d1.category, d1.color, d1.iccid, d1.status),
                         ('Trucks', 'truck', '#ff0000', '8920', 'online'))
        self.assertTrue(d1.last_update)
        self.assertFalse(devs.filtered(lambda d: d.traccar_id == 2).vehicle_id)
        self.assertEqual(self.vehicle.gps_device_id, d1)

    def test_ambiguous_plate_is_not_linked(self):
        self.env['fleet.vehicle'].create({'model_id': self.model.id, 'license_plate': 'abc123'})
        dev = self.env['fleet.gps.device'].create({'name': 'ABC123', 'server_id': self.server.id, 'traccar_id': 9})
        dev._auto_link_vehicle()
        self.assertFalse(dev.vehicle_id)

    def test_one_vehicle_one_active_device(self):
        Dev = self.env['fleet.gps.device']
        Dev.create({'name': 'a', 'server_id': self.server.id, 'traccar_id': 11, 'vehicle_id': self.vehicle.id})
        with self.assertRaises(Exception), self.cr.savepoint():
            Dev.create({'name': 'b', 'server_id': self.server.id, 'traccar_id': 12, 'vehicle_id': self.vehicle.id})

    def test_client_error_is_user_error_without_secrets(self):
        with patch(CLIENT_GET, side_effect=TraccarError('network', "The Traccar server could not be reached.")):
            with self.assertRaises(UserError) as cm:
                self.server._call('devices')
        self.assertNotIn('S3CRET', str(cm.exception))


@tagged('post_install', '-at_install')
class TestIngest(Common):

    def test_ingest_dedupes_converts_and_updates_latest(self):
        Pos = self.env['fleet.gps.position']
        info = {1: {'id': 1, 'name': 'New one', 'uniqueId': 'u1'}}
        n = Pos._ingest(self.server, [_pos(1, speed=10, batteryLevel=80, ignition=True),
                                      _pos(2, fix='2026-11-01T10:00:10.000+00:00', speed=20)], info)
        self.assertEqual(n, 2)
        self.assertEqual(Pos._ingest(self.server, [_pos(2)], info), 0)  # duplicate
        dev = self.env['fleet.gps.device'].search([('traccar_id', '=', 1)])
        self.assertEqual(dev.name, 'New one')  # auto-created from the pushed device
        self.assertEqual(dev.last_position_id, 2)
        self.assertAlmostEqual(dev.last_speed, 20 * 1.852, places=1)
        first = Pos.search([('traccar_id', '=', 1)])
        self.assertTrue(first.ignition)
        self.assertEqual(first.battery, 80)
        self.assertEqual(first.company_id, self.server.company_id)

    def test_clock_ahead_fix_never_becomes_latest(self):
        Pos = self.env['fleet.gps.position']
        Pos._ingest(self.server, [_pos(1)], {})
        Pos._ingest(self.server, [_pos(2, fix='2026-11-01T18:00:00.000+00:00', speed=99)], {})  # +8h clock
        dev = self.env['fleet.gps.device'].search([('traccar_id', '=', 1)])
        self.assertEqual(dev.last_position_id, 1)
        self.assertEqual(Pos.search_count([('device_id', '=', dev.id)]), 2)  # still stored

    def test_older_position_does_not_replace_latest(self):
        Pos = self.env['fleet.gps.position']
        Pos._ingest(self.server, [_pos(5)], {})
        Pos._ingest(self.server, [_pos(3, speed=50)], {})
        self.assertEqual(self.env['fleet.gps.device'].search([('traccar_id', '=', 1)]).last_position_id, 5)

    def test_rows_are_inserted_in_a_fixed_order(self):
        """Overlapping batches from the push, the poll and a history import must lock rows in the same order."""
        Pos = self.env['fleet.gps.position']
        info = {1: {'id': 1, 'name': 'A', 'uniqueId': 'a'}, 2: {'id': 2, 'name': 'B', 'uniqueId': 'b'}}
        sent = []
        from odoo.addons.elblasy_fleet_gps.models import gps_position
        original = gps_position.execute_values

        def spy(cr, query, argslist, *a, **k):
            sent.append([(r[0], r[2]) for r in argslist])
            return original(cr, query, argslist, *a, **k)
        with patch.object(gps_position, 'execute_values', spy):
            Pos._ingest(self.server, [_pos(9, dev=2), _pos(5, dev=1), _pos(7, dev=2), _pos(3, dev=1)], info)
        self.assertEqual(sent[0], sorted(sent[0]))

    def test_bad_rows_are_skipped(self):
        Pos = self.env['fleet.gps.position']
        self.assertEqual(Pos._ingest(self.server, [{'id': 1}, {'deviceId': 1}, 'x', _pos(1, fix='garbage')], {}), 0)

    def test_purge(self):
        Pos = self.env['fleet.gps.position']
        Pos._ingest(self.server, [_pos(1, fix='2020-01-01T00:00:00.000+00:00', srv='2020-01-01T00:00:01.000+00:00')], {})
        with nullcontext():
            Pos._cron_purge()
        self.assertFalse(Pos.search([]))

    def test_poll_cron_uses_positions_and_survives_down_server(self):
        def fake(path, params=None):
            if path == 'devices':
                return [{'id': 1, 'name': 'P', 'uniqueId': 'p', 'status': 'online'}]
            return [_pos(1)]
        with patch(CLIENT_GET, side_effect=fake), nullcontext():
            self.env['fleet.gps.server']._cron_poll()
        self.assertEqual(self.env['fleet.gps.device'].search([('traccar_id', '=', 1)]).last_position_id, 1)
        with patch(CLIENT_GET, side_effect=TraccarError('network', 'down')), \
                nullcontext():
            self.env['fleet.gps.server']._cron_poll()  # must not raise


@tagged('post_install', '-at_install')
class TestSecurity(Common):

    def test_user_cannot_read_secrets_or_write(self):
        user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'GU', 'login': 'gu', 'email': 'gu@example.com',
            'groups_id': [(6, 0, [self.env.ref('elblasy_fleet_gps.group_gps_user').id])]})
        srv = self.server.with_user(user)
        self.assertEqual(srv.name, 'T')
        with self.assertRaises(AccessError):
            srv.api_token
        with self.assertRaises(AccessError):
            srv.write({'name': 'x'})
        with self.assertRaises(AccessError):
            self.env['fleet.gps.device'].with_user(user).create(
                {'name': 'd', 'server_id': self.server.id, 'traccar_id': 1})


@tagged('post_install', '-at_install')
class TestPushEndpoint(HttpCase):

    def setUp(self):
        super().setUp()
        self.server = self.env['fleet.gps.server'].create({'name': 'T', 'base_url': 'http://traccar.test'})
        self.token = self.server.sudo().push_token
        self.env.cr.flush()

    def _post(self, token, body, raw=None):
        return self.url_open(f'/fleet_gps/push/{token}', data=raw if raw is not None else json.dumps(body),
                             headers={'Content-Type': 'application/json'})

    def test_push_stores_position(self):
        r = self._post(self.token, {'position': _pos(1, speed=5), 'device': {'id': 1, 'name': 'Pushed', 'uniqueId': 'x', 'status': 'online'}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['stored'], 1)
        dev = self.env['fleet.gps.device'].search([('traccar_id', '=', 1)])
        self.assertEqual((dev.name, dev.status), ('Pushed', 'online'))
        self.assertTrue(self.server.last_push_date)

    def test_push_accepts_a_batch(self):
        r = self._post(self.token, [{'position': _pos(1)}, {'position': _pos(2, fix='2026-11-01T10:00:05.000+00:00')}])
        self.assertEqual(r.json()['stored'], 2)

    def test_bad_token_and_bad_json(self):
        self.assertEqual(self._post('nope', {}).status_code, 404)
        self.assertEqual(self._post(self.token, None, raw='{not json').status_code, 400)
