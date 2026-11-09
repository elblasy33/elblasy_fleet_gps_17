from contextlib import nullcontext
from datetime import date, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, tagged

from odoo.addons.elblasy_fleet_gps.models import analytics
from .test_gps import Common

GET = 'odoo.addons.elblasy_fleet_gps.models.traccar_client.TraccarClient.get'
M_PER_DEG = 111195.0


def track(start_ms, legs, lat=30.0, lon=31.0, step=10000):
    """legs: [(seconds, kmh, ignition)] -> rows (ms, lat, lon, kmh, ign), moving north at the leg's speed."""
    rows, t = [(start_ms, lat, lon, legs[0][1], legs[0][2])], start_ms
    for secs, kmh, ign in legs:
        for _ in range(int(secs * 1000 // step)):
            t += step
            lat += (kmh / 3.6 * step / 1000) / M_PER_DEG
            rows.append((t, lat, lon, kmh, ign))
    return rows


@tagged('post_install', '-at_install')
class TestEngine(TransactionCase):

    hour = staticmethod(lambda ms: 10)

    def test_trip_then_stop_then_second_trip_after_gap(self):
        t0 = 1_800_000_000_000
        first = track(t0, [(300, 50, True), (300, 0, False)])            # drives 5 min, then parked 5 min
        second = track(first[-1][0] + 20 * 60000, [(120, 40, True)], lat=first[-1][1])  # 20 min gap, drives again
        g = analytics.compute(first + second, 80, self.hour)
        self.assertEqual(g['trips'], 2)
        self.assertAlmostEqual(g['trip_list'][0]['dist'], 50 / 3.6 * 300, delta=60)
        self.assertEqual(len(g['stop_list']), 1)
        self.assertGreaterEqual(g['stop_list'][0]['e'] - g['stop_list'][0]['s'], analytics.STOP_MIN_MS)
        self.assertFalse(g['stop_list'][0]['ign'])
        self.assertEqual(g['max_speed'], 50)
        self.assertAlmostEqual(g['moving_ms'], 420000, delta=20000)
        self.assertEqual(g['hour_moving'][10], g['moving_ms'])
        self.assertEqual(g['over_count'], 0)

    def test_overspeed_counts_events_not_samples(self):
        rows = track(0, [(60, 100, True), (60, 30, True), (60, 100, True)])
        g = analytics.compute(rows, 80, self.hour)
        self.assertEqual(g['over_count'], 2)
        self.assertAlmostEqual(g['over_ms'], 120000, delta=20000)

    def test_idle_vs_stopped_and_short_movement_is_not_a_trip(self):
        g = analytics.compute(track(0, [(240, 0, True), (60, 0, False)]), 80, self.hour)
        self.assertEqual((g['trips'], g['moving_ms']), (0, 0))
        self.assertAlmostEqual(g['idle_ms'], 240000, delta=10000)
        self.assertAlmostEqual(g['stop_ms'], 60000, delta=10000)
        drift = analytics.compute(track(0, [(20, 20, True)]), 80, self.hour)  # ~110 m
        self.assertEqual(drift['trips'], 0)

    def test_glitches_are_ignored(self):
        rows = track(0, [(60, 40, True)])
        rows[3] = (rows[3][0], rows[3][1] + 5, rows[3][2], 400, True)  # 400 km/h and 550 km jump
        g = analytics.compute(rows, 80, self.hour)
        self.assertLess(g['max_speed'], 220)
        self.assertLess(g['dist'], 3000)

    def test_harsh_braking_and_acceleration(self):
        t = 1_800_000_000_000  # real epoch milliseconds: the "5 s since the last event" rule starts from 0
        rows = [(t, 30.0, 31.0, 60, True), (t + 5000, 30.001, 31.0, 20, True),   # -8 km/h per s
                (t + 10000, 30.002, 31.0, 20, True), (t + 15000, 30.003, 31.0, 60, True)]  # +8 km/h per s
        g = analytics.compute(rows, 80, self.hour)
        self.assertEqual((g['harsh_brake'], g['harsh_accel']), (1, 1))

    def test_unsorted_or_duplicate_times_do_not_crash_and_zero_coords_dropped(self):
        rows = [(1000, 0, 0, 50, True), (2000, 30, 31, 50, True), (2000, 30, 31, 50, True), (1500, 30, 31, 50, True)]
        g = analytics.compute(rows, 80, self.hour)
        self.assertEqual(g['points'], 3)

    def test_driver_score(self):
        self.assertEqual(analytics.driver_score(100000, 3600000, 0, 0), 100)
        self.assertEqual(analytics.driver_score(100000, 3600000, 3600000, 0), 40)
        self.assertEqual(analytics.driver_score(10000, 3600000, 0, 100), 60)


def _p(pid, dev, fix, lat, speed_kmh, ign=True):
    return {'id': pid, 'deviceId': dev, 'fixTime': fix, 'serverTime': fix, 'latitude': lat, 'longitude': 31.0,
            'speed': speed_kmh / 1.852, 'course': 0, 'valid': True, 'attributes': {'ignition': ign}}


@tagged('post_install', '-at_install')
class TestDaily(Common):

    def setUp(self):
        super().setUp()
        self.dev = self.env['fleet.gps.device'].create({'name': 'D', 'server_id': self.server.id, 'traccar_id': 1,
                                                        'vehicle_id': self.vehicle.id})
        self.Daily = self.env['fleet.gps.daily']

    def _drive(self, first_id, start_iso_minute, minutes=5, kmh=50, base_lat=30.0):
        """A positions list for a drive that starts at 2026-11-01 HH:MM (UTC)."""
        out, lat = [], base_lat
        for i in range(minutes * 6 + 1):
            h, m = divmod(start_iso_minute + (i * 10) // 60, 60)
            sec = (i * 10) % 60
            out.append(_p(first_id + i, 1, f'2026-11-01T{h:02d}:{m:02d}:{sec:02d}.000+00:00', lat, kmh))
            lat += (kmh / 3.6 * 10) / M_PER_DEG
        return out

    def test_compute_day_creates_summary_trips_stops_in_local_day(self):
        Pos = self.env['fleet.gps.position']
        Pos._ingest(self.server, self._drive(1, 8 * 60), {})                      # 08:00Z = 10:00 Cairo on Nov 1
        Pos._ingest(self.server, [_p(900, 1, '2026-10-31T21:30:00.000+00:00', 30.5, 50)], {})  # 23:30 Cairo on Oct 31
        rec = self.Daily._compute_for(self.dev, date(2026, 11, 1))
        self.assertEqual(rec.points, 31)  # the Oct 31 (Cairo) point belongs to the previous day
        self.assertEqual(rec.trip_count, 1)
        self.assertAlmostEqual(rec.distance_km, 50 / 3.6 * 300 / 1000, delta=0.1)
        self.assertEqual(rec.max_speed, 50)
        self.assertEqual(rec.vehicle_id, self.vehicle)
        self.assertEqual(rec.hour_moving[10] > 0 and rec.hour_moving[8] == 0, True)  # local hour 10, not UTC hour 8
        self.assertEqual(rec.score, 100)
        self.assertEqual(len(rec.trip_ids), 1)
        self.assertGreater(rec.trip_ids.duration_minutes, 4)
        self.assertTrue(rec.final)  # that day is long over

    def test_recompute_replaces_children_and_final_days_are_kept(self):
        Pos = self.env['fleet.gps.position']
        Pos._ingest(self.server, self._drive(1, 8 * 60), {})
        rec = self.Daily._compute_for(self.dev, date(2026, 11, 1))
        trip_ids = rec.trip_ids.ids
        again = self.Daily._compute_for(self.dev, date(2026, 11, 1), force=True)
        self.assertEqual(again, rec)
        self.assertEqual(len(again.trip_ids), 1)
        self.assertNotEqual(again.trip_ids.ids, trip_ids)  # rebuilt, not duplicated
        Pos._ingest(self.server, self._drive(500, 12 * 60), {})
        kept = self.Daily._compute_for(self.dev, date(2026, 11, 1))  # final: left alone without force
        self.assertEqual(kept.trip_count, 1)
        self.assertEqual(self.Daily._compute_for(self.dev, date(2026, 11, 1), force=True).trip_count, 2)

    def test_day_without_data_creates_nothing(self):
        self.assertFalse(self.Daily._compute_for(self.dev, date(2026, 11, 2)))
        self.assertEqual(self.Daily.search_count([('device_id', '=', self.dev.id)]), 0)

    def test_timezone_setting_moves_the_day_boundary(self):
        self.env['fleet.gps.position']._ingest(self.server, [_p(1, 1, '2026-11-01T01:00:00.000+00:00', 30.0, 50),
                                                              _p(2, 1, '2026-11-01T01:00:10.000+00:00', 30.001, 50)], {})
        self.assertEqual(self.Daily._compute_for(self.dev, date(2026, 11, 1)).points, 2)   # 03:00 Cairo
        self.env['ir.config_parameter'].set_param('elblasy_fleet_gps.timezone', 'America/New_York')
        self.assertFalse(self.Daily._compute_for(self.dev, date(2026, 11, 1), force=True).points)  # 21:00 on Oct 31 in New York

    def test_cron_computes_today_and_yesterday(self):
        now = fields.Datetime.now()
        iso = lambda dt: dt.strftime('%Y-%m-%dT%H:%M:%S.000+00:00')  # noqa: E731
        self.env['fleet.gps.position']._ingest(self.server, [
            _p(1, 1, iso(now - timedelta(minutes=2)), 30.0, 50), _p(2, 1, iso(now - timedelta(minutes=1)), 30.002, 50)], {})
        with nullcontext():
            self.Daily._cron_compute()
        self.assertTrue(self.Daily.search([('device_id', '=', self.dev.id)]))


@tagged('post_install', '-at_install')
class TestBackfill(Common):

    def setUp(self):
        super().setUp()
        self.dev = self.env['fleet.gps.device'].create({'name': 'D', 'server_id': self.server.id, 'traccar_id': 1})
        self.job = self.env['fleet.gps.backfill'].create({
            'server_id': self.server.id, 'date_from': date(2026, 11, 1), 'date_to': date(2026, 11, 2)})

    def _route(self, path, params=None):
        self.assertEqual(path, 'reports/route')
        self.assertEqual(params['deviceId'], 1)
        if params['from'].startswith('2026-10-31T22'):  # Cairo midnight of Nov 1
            return [_p(i, 1, f'2026-11-01T08:00:{i * 10:02d}.000+00:00', 30.0 + i * 0.001, 50) for i in range(1, 6)]
        return []

    def test_job_runs_in_batches_and_builds_summaries(self):
        self.job.action_start()
        self.assertEqual((self.job.state, self.job.total), ('running', 2))
        with patch(GET, side_effect=self._route), nullcontext():
            self.job._cron_step()
        self.assertEqual((self.job.state, self.job.cursor, self.job.positions), ('done', 2, 5))
        self.assertEqual(self.job.progress, 100)
        daily = self.env['fleet.gps.daily'].search([('device_id', '=', self.dev.id)])
        self.assertEqual(daily.mapped('date'), [date(2026, 11, 1)])  # the empty day created nothing
        self.assertFalse(self.server.last_push_date)  # a history import is not live traffic

    def test_batch_size_is_limited_and_resumes(self):
        job = self.env['fleet.gps.backfill'].create({'server_id': self.server.id, 'date_from': date(2026, 11, 1), 'date_to': date(2026, 11, 30)})
        job.action_start()
        with patch(GET, return_value=[]), nullcontext():
            job._step(steps=5)
            self.assertEqual((job.state, job.cursor), ('running', 5))
            job._step(steps=100)
        self.assertEqual((job.state, job.cursor), ('done', 30))

    def test_traccar_failure_marks_job_failed_with_message(self):
        from odoo.addons.elblasy_fleet_gps.models.traccar_client import TraccarError
        self.job.action_start()
        with patch(GET, side_effect=TraccarError('network', 'The Traccar server could not be reached.')), nullcontext():
            self.job._step()
        self.assertEqual(self.job.state, 'failed')
        self.assertIn('could not be reached', self.job.error)

    def test_range_is_validated(self):
        from odoo.exceptions import UserError
        self.job.date_to = date(2027, 6, 1)
        with self.assertRaises(UserError):
            self.job.action_start()


@tagged('post_install', '-at_install')
class TestReportsShowDevices(Common):
    """Devices are often not linked to a vehicle yet: the reports must still show one row per device, not "None"."""

    def test_grouping_by_device_works_without_vehicles(self):
        dev = self.env['fleet.gps.device'].create({'name': '1969', 'server_id': self.server.id, 'traccar_id': 1})
        self.env['fleet.gps.position']._ingest(self.server, [
            _p(1, 1, '2026-11-01T08:00:00.000+00:00', 30.0, 50), _p(2, 1, '2026-11-01T08:00:10.000+00:00', 30.001, 50)], {})
        self.env['fleet.gps.daily']._compute_for(dev, date(2026, 11, 1))
        rows = self.env['fleet.gps.daily']._read_group([], ['device_id'], ['distance_km:sum'])
        self.assertEqual([(dev.display_name, round(km, 1)) for dev, km in rows], [('1969', 0.1)])
        for xml_id in ('gps_daily_view_pivot', 'gps_daily_view_list', 'gps_trip_view_list', 'gps_stop_view_list'):
            arch = self.env.ref(f'elblasy_fleet_gps.{xml_id}').arch
            self.assertIn('name="device_id"', arch, xml_id)


@tagged('post_install', '-at_install')
class TestDashboard(Common):

    def setUp(self):
        super().setUp()
        self.Dash = self.env['fleet.gps.dashboard']
        Dev = self.env['fleet.gps.device']
        self.a = Dev.create({'name': 'A', 'server_id': self.server.id, 'traccar_id': 1, 'status': 'online', 'last_battery': 12})
        self.b = Dev.create({'name': 'B', 'server_id': self.server.id, 'traccar_id': 2, 'status': 'offline'})
        pts = [_p(i, 1, f'2026-11-01T08:{i // 6:02d}:{(i % 6) * 10:02d}.000+00:00', 30.0 + i * 0.0012, 100) for i in range(0, 61)]
        self.env['fleet.gps.position']._ingest(self.server, pts, {})
        self.env['fleet.gps.daily']._compute_for(self.a, date(2026, 11, 1))
        self.env['fleet.gps.event']._ingest(self.server, [
            {'id': 1, 'type': 'alarm', 'deviceId': 1, 'eventTime': '2026-11-01T09:00:00.000+00:00', 'attributes': {'alarm': 'sos'}},
            {'id': 2, 'type': 'ignitionOn', 'deviceId': 1, 'eventTime': '2026-11-01T09:00:00.000+00:00', 'attributes': {}}])

    def test_numbers_match_the_daily_rows(self):
        data = self.Dash.get_data('2026-11-01', '2026-11-03')
        k = data['kpi']
        daily = self.env['fleet.gps.daily'].search([('device_id', '=', self.a.id)])
        self.assertAlmostEqual(k['distance_km'], daily.distance_km, places=3)
        self.assertGreater(k['distance_km'], 5)
        self.assertEqual((k['vehicles'], k['events'], k['trips']), (2, 2, 1))
        self.assertEqual(k['max_speed'], 100)
        self.assertGreater(k['over_count'], 0)  # 100 km/h over the 80 limit
        self.assertEqual([d['date'] for d in data['daily']], ['2026-11-01', '2026-11-02', '2026-11-03'])
        self.assertEqual(data['daily'][1]['km'], 0)  # a day without data is a zero, not a gap
        self.assertAlmostEqual(sum(data['speed_hist']), daily.moving_hours + daily.idle_hours + daily.stopped_hours, delta=0.05)
        self.assertEqual(data['by_device'][0]['name'], 'A')
        self.assertEqual({e['type'] for e in data['event_types']}, {'alarm', 'ignitionOn'})
        self.assertEqual([b['name'] for b in data['low_battery']], ['A'])
        self.assertEqual(len(data['trips']), 1)
        self.assertTrue(data['has_data'])

    def test_hourly_uses_the_local_hour(self):
        h = self.Dash.get_data('2026-11-01', '2026-11-01')['hourly']
        self.assertGreater(h[10], 0)  # 08:xx UTC is 10:xx in Cairo
        self.assertEqual(h[8], 0)

    def test_device_filter_and_empty_range(self):
        only_b = self.Dash.get_data('2026-11-01', '2026-11-03', [self.b.id])
        self.assertEqual((only_b['kpi']['vehicles'], only_b['kpi']['distance_km'], only_b['has_data']), (1, 0, False))
        self.assertEqual(only_b['live']['offline'], 1)
        empty = self.Dash.get_data('2026-06-01', '2026-06-02')
        self.assertEqual((empty['kpi']['score'], empty['kpi']['avg_speed'], empty['kpi']['max_speed']), (0, 0, 0))

    def test_range_is_validated_and_access_is_enforced(self):
        from odoo.exceptions import AccessError, UserError
        with self.assertRaises(UserError):
            self.Dash.get_data('2026-11-05', '2026-11-01')
        with self.assertRaises(UserError):
            self.Dash.get_data('2026-01-01', '2026-11-01')
        outsider = self.env['res.users'].with_context(no_reset_password=True).create({'name': 'N3', 'login': 'nogps3', 'email': 'n3@example.com'})
        with self.assertRaises(AccessError):
            self.Dash.with_user(outsider).get_data('2026-11-01', '2026-11-02')


@tagged('post_install', '-at_install')
class TestReportsExtras(Common):

    def setUp(self):
        super().setUp()
        self.fence = self.env['fleet.gps.geofence'].create({'name': 'Yard', 'server_id': self.server.id, 'traccar_id': 1,
                                                            'area': 'CIRCLE (30.0 31.0, 300)'})
        self.dev = self.env['fleet.gps.device'].create({'name': 'A', 'server_id': self.server.id, 'traccar_id': 1, 'status': 'online'})

    def test_geofence_contains_circle_and_polygon(self):
        Fence = self.env['fleet.gps.geofence']
        self.assertTrue(self.fence._contains(30.001, 31.001))     # ~150 m away
        self.assertFalse(self.fence._contains(30.01, 31.0))       # ~1.1 km away
        poly = Fence.create({'name': 'P', 'server_id': self.server.id, 'traccar_id': 2, 'area': 'POLYGON ((30 31, 30 32, 31 32, 31 31))'})
        self.assertTrue(poly._contains(30.5, 31.5))
        self.assertFalse(poly._contains(32, 31.5))
        for bad in ('LINESTRING (30 31, 31 32)', 'junk', False, 'CIRCLE (x y, z)'):
            self.assertFalse(Fence.create({'name': 'B', 'server_id': self.server.id, 'traccar_id': 10 + len(str(bad)), 'area': bad})._contains(30, 31))

    def _stops(self):
        rows = [_p(i, 1, f'2026-11-01T08:{i // 6:02d}:{(i % 6) * 10:02d}.000+00:00', 30.0005, 0, ign=bool(i % 2)) for i in range(0, 70)]
        rows += [_p(100 + i, 1, f'2026-11-01T10:{i // 6:02d}:{(i % 6) * 10:02d}.000+00:00', 30.5, 0, ign=False) for i in range(0, 40)]
        self.env['fleet.gps.position']._ingest(self.server, rows, {})
        return self.env['fleet.gps.daily']._compute_for(self.dev, date(2026, 11, 1)).stop_ids.sorted('start_time')

    def test_stops_get_geofence_or_rounded_place(self):
        stops = self._stops()
        self.assertEqual(len(stops), 2)
        self.assertEqual((stops[0].place, stops[0].geofence_id), ('Yard', self.fence))
        self.assertEqual((stops[1].place, stops[1].geofence_id.id), ('30.500, 31.000', False))

    def test_nearby_stops_share_one_place_far_ones_do_not(self):
        Stop = self.env['fleet.gps.stop']
        known = []
        a = Stop._place_vals(30.5000, 31.0000, [], known)
        b = Stop._place_vals(30.5010, 31.0000, [], known)   # ~110 m north
        c = Stop._place_vals(30.5030, 31.0000, [], known)   # ~330 m north of the first: a different place
        self.assertEqual(a['place'], b['place'])
        self.assertNotEqual(a['place'], c['place'])
        daily = self.env['fleet.gps.daily'].create({'device_id': self.dev.id, 'date': date(2026, 11, 2)})
        Stop.create({'daily_id': daily.id, 'start_time': '2026-11-02 08:00:00', 'end_time': '2026-11-02 09:00:00',
                     'latitude': 30.5, 'longitude': 31.0, **a})
        d = Stop._place_vals(30.5008, 31.0, [], [])  # a stop stored earlier is found in the database, not only in memory
        self.assertEqual(d['place'], a['place'])

    def test_places_are_assigned_to_old_stops(self):
        stops = self._stops()
        stops.write({'place': False, 'geofence_id': False})
        self.env['fleet.gps.stop']._assign_places()
        self.assertEqual(stops.sorted('start_time').mapped('place'), ['Yard', '30.500, 31.000'])

    def test_waiting_report_groups_stops_by_place(self):
        self._stops()
        rows = self.env['fleet.gps.stop']._read_group([], ['place'], ['duration_minutes:sum', '__count'])
        self.assertEqual(sorted(r[0] for r in rows), ['30.500, 31.000', 'Yard'])

    def test_every_report_view_and_action_loads(self):
        for xml_id in ('gps_behavior_action', 'gps_waiting_action', 'gps_compare_action'):
            act = self.env.ref(f'elblasy_fleet_gps.{xml_id}')
            views = self.env[act.res_model].get_views([(v.view_id.id, v.view_mode) for v in act.view_ids] or [(False, m) for m in act.view_mode.split(',')])
            self.assertTrue(views['views'], xml_id)

    def test_insights_and_heatmap(self):
        Dash = self.env['fleet.gps.dashboard']
        pts = [_p(i, 1, f'2026-11-01T08:{i // 6:02d}:{(i % 6) * 10:02d}.000+00:00', 30.0 + i * 0.0012, 100) for i in range(0, 61)]
        self.env['fleet.gps.position']._ingest(self.server, pts, {})
        self.env['fleet.gps.daily']._compute_for(self.dev, date(2026, 11, 1))
        prev = [_p(500 + i, 1, f'2026-10-31T08:{i // 6:02d}:{(i % 6) * 10:02d}.000+00:00', 30.0 + i * 0.0006, 50) for i in range(0, 61)]
        self.env['fleet.gps.position']._ingest(self.server, prev, {})
        self.env['fleet.gps.daily']._compute_for(self.dev, date(2026, 10, 31))
        self.dev.write({'last_update': fields.Datetime.now() - timedelta(days=3), 'status': 'offline'})
        data = Dash.get_data('2026-11-01', '2026-11-01')
        text = ' | '.join(i['text'] for i in data['insights'])
        self.assertIn('Most driven: A', text)
        self.assertIn('went over 80 km/h', text)
        self.assertIn('have not reported for over 24 hours', text)
        self.assertIn('compared with the previous 1 days', text)
        self.assertLessEqual(len(data['insights']), 8)
        heat = data['heatmap']
        self.assertEqual((heat['mode'], len(heat['rows']), len(heat['rows'][0]['values'])), ('day', 1, 24))
        self.assertGreater(heat['max'], 0)
        self.assertGreater(heat['rows'][0]['values'][10], 0)  # local hour, not UTC
        wk = Dash.get_data('2026-09-01', '2026-11-01')['heatmap']
        self.assertEqual((wk['mode'], len(wk['rows'])), ('weekday', 7))
        empty = Dash.get_data('2026-06-01', '2026-06-01')['insights']
        self.assertEqual([i['text'] for i in empty if 'driven' in i['text'] or 'compared' in i['text']], [])  # no trips, no trip findings
        self.assertTrue(any('not reported' in i['text'] for i in empty))  # a silent vehicle matters whatever the range
