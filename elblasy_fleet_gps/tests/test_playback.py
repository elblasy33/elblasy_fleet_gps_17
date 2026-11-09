from datetime import date, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .test_analytics import _p
from .test_gps import Common

GET = 'odoo.addons.elblasy_fleet_gps.models.traccar_client.TraccarClient.get'


@tagged('post_install', '-at_install')
class TestRoute(Common):

    def setUp(self):
        super().setUp()
        self.dev = self.env['fleet.gps.device'].create({'name': 'R', 'server_id': self.server.id, 'traccar_id': 1,
                                                        'vehicle_id': self.vehicle.id, 'category': 'container'})
        pts = [_p(i + 1, 1, f'2026-11-01T08:{i // 6:02d}:{(i % 6) * 10:02d}.000+00:00', 30.0 + i * 0.001, 50 if i < 40 else 0) for i in range(0, 60)]
        glitch = _p(500, 1, '2026-11-01T08:30:00.000+00:00', 0, 0)
        glitch['longitude'] = 0  # a (0, 0) fix is a tracker without GPS, not a place
        pts += [glitch]
        self.env['fleet.gps.position']._ingest(self.server, pts, {})
        self.env['fleet.gps.daily']._compute_for(self.dev, date(2026, 11, 1))
        self.Pos = self.env['fleet.gps.position']

    def test_compact_route_for_a_local_day(self):
        r = self.Pos.get_route(self.dev.id, day='2026-11-01')
        self.assertEqual(r['source'], 'stored')
        self.assertEqual(len(r['pts']), 60)  # the (0, 0) point is dropped
        ms, lat, lon, kmh, course, ign = r['pts'][0]
        self.assertEqual((lat, lon, kmh), (30.0, 31.0, 50.0))
        self.assertEqual([p[0] for p in r['pts']], sorted(p[0] for p in r['pts']))
        self.assertEqual(r['device']['plate'], 'ABC 123')
        self.assertEqual(r['device']['category'], 'container')
        self.assertEqual(r['window'][0], '2026-10-31 22:00:00')  # Cairo midnight
        self.assertEqual(r['limit'], 80)
        self.assertEqual(r['icon_size'], 140)
        self.assertEqual(len(r['trips']), 1)
        self.assertEqual(len(r['stops']), 1)
        self.assertGreaterEqual(r['stops'][0]['minutes'], 3)

    def test_window_by_utc_range_and_validation(self):
        r = self.Pos.get_route(self.dev.id, '2026-11-01 08:02:00', '2026-11-01 08:04:00')
        self.assertTrue(0 < len(r['pts']) < 20)
        with self.assertRaises(UserError):
            self.Pos.get_route(self.dev.id, '2026-11-05 00:00:00', '2026-11-01 00:00:00')
        with self.assertRaises(UserError):
            self.Pos.get_route(self.dev.id, '2026-10-01 00:00:00', '2026-11-01 00:00:00')
        with self.assertRaises(UserError):
            self.Pos.get_route(999999, day='2026-11-01')

    def test_a_day_without_stored_positions_is_read_from_traccar_and_not_stored(self):
        remote = [{'id': i, 'deviceId': 1, 'fixTime': f'2026-10-20T10:00:{i * 10:02d}.000+00:00', 'serverTime': '2026-10-20T10:01:00.000+00:00',
                   'latitude': 30.0 + i * 0.001, 'longitude': 31.0, 'speed': 27.0, 'course': 90, 'attributes': {'ignition': True}} for i in range(5)]
        before = self.Pos.search_count([])
        with patch(GET, return_value=remote) as get:
            r = self.Pos.get_route(self.dev.id, day='2026-10-20')
        self.assertEqual((r['source'], len(r['pts'])), ('traccar', 5))
        self.assertAlmostEqual(r['pts'][0][3], 27.0 * 1.852, places=1)
        self.assertEqual(r['pts'][0][5], 1)
        self.assertEqual(get.call_args.args[0], 'reports/route')
        self.assertEqual(self.Pos.search_count([]), before)

    def test_traccar_down_gives_an_empty_route_not_an_error(self):
        from odoo.addons.elblasy_fleet_gps.models.traccar_client import TraccarError
        with patch(GET, side_effect=TraccarError('network', 'down')):
            r = self.Pos.get_route(self.dev.id, day='2026-10-20')
        self.assertEqual((r['pts'], r['source']), ([], 'traccar'))

    def test_huge_routes_are_decimated_but_keep_the_last_point(self):
        full = self.Pos.get_route(self.dev.id, day='2026-11-01')['pts']
        with patch('odoo.addons.elblasy_fleet_gps.models.gps_position.MAX_POINTS', 10):
            thin = self.Pos.get_route(self.dev.id, day='2026-11-01')
        self.assertLessEqual(len(thin['pts']), 11)
        self.assertEqual(thin['pts'][-1], full[-1])  # the end of the route is never cut off
        self.assertEqual(thin['pts'][0], full[0])

    def test_access_is_enforced_and_actions_target_the_playback(self):
        outsider = self.env['res.users'].with_context(no_reset_password=True).create({'name': 'N4', 'login': 'nogps4', 'email': 'n4@example.com'})
        with self.assertRaises(AccessError):
            self.Pos.with_user(outsider).get_route(self.dev.id, day='2026-11-01')
        act = self.dev.action_playback()
        self.assertEqual((act['tag'], act['params']['device_id']), ('elblasy_fleet_gps_playback', self.dev.id))
        trip = self.env['fleet.gps.trip'].search([('device_id', '=', self.dev.id)], limit=1)
        play = trip.action_playback()
        self.assertEqual(play['params']['device_id'], self.dev.id)
        self.assertLess(play['params']['start'], str(trip.start_time))  # padded by two minutes
        self.assertGreater(play['params']['end'], str(trip.end_time))
