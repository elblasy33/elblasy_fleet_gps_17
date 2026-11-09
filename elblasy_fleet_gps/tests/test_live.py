from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.elblasy_fleet_gps.models import gps_device
from odoo.addons.elblasy_fleet_gps.models.traccar_client import TraccarError
from .test_gps import Common, _pos


@tagged('post_install', '-at_install')
class TestLiveData(Common):

    def setUp(self):
        super().setUp()
        self.Dev = self.env['fleet.gps.device']
        self.dev = self.Dev.create({'name': 'ABC 123', 'server_id': self.server.id, 'traccar_id': 1,
                                    'vehicle_id': self.vehicle.id, 'status': 'online'})
        self.env['fleet.gps.position']._ingest(self.server, [_pos(1, speed=10, ignition=True)], {})

    def test_full_payload(self):
        res = self.Dev.get_live_data()
        self.assertTrue(res['full'])
        self.assertEqual(res['config']['speed_limit'], 80)
        self.assertEqual(set(res['config']['colors']), {'moving', 'idle', 'stopped', 'over', 'offline'})
        d, = res['devices']
        self.assertEqual((d['plate'], d['vehicle_id'], d['status'], d['category']), ('ABC 123', self.vehicle.id, 'online', 'default'))
        self.assertAlmostEqual(d['speed'], 18.52, places=1)
        self.assertEqual((d['lat'], d['lon'], d['ignition']), (30.0, 31.0, True))

    def test_delta_returns_only_what_changed(self):
        res = self.Dev.get_live_data()
        quiet = self.Dev.create({'name': 'Quiet', 'server_id': self.server.id, 'traccar_id': 2})
        quiet.flush_recordset()
        self.env.cr.execute("UPDATE fleet_gps_device SET write_date = (now() AT TIME ZONE 'UTC') - interval '1 hour' WHERE id = %s", [quiet.id])
        self.env.invalidate_all()
        self.assertEqual(sorted(d['name'] for d in self.Dev.get_live_data(since=res['now'])['devices']), ['ABC 123'])
        self.assertFalse(self.Dev.get_live_data(since=res['now'])['full'])
        # a position arriving after the cursor shows up in the next delta
        self.env['fleet.gps.position']._ingest(self.server, [_pos(2, fix='2026-11-01T10:00:09.000+00:00')], {})
        self.assertEqual([d['name'] for d in self.Dev.get_live_data(since=res['now'])['devices']], ['ABC 123'])

    def test_disabled_devices_hidden(self):
        self.dev.disabled = True
        self.assertFalse(self.Dev.get_live_data()['devices'])

    def test_settings_feed_config(self):
        self.env['ir.config_parameter'].set_param('elblasy_fleet_gps.speed_limit', '100')
        self.env['ir.config_parameter'].set_param('elblasy_fleet_gps.color_over', '#000000')
        cfg = self.Dev.get_live_data()['config']
        self.assertEqual((cfg['speed_limit'], cfg['colors']['over']), (100, '#000000'))

    def test_icon_size_defaults_to_140_and_is_clamped(self):
        params = self.env['ir.config_parameter']
        self.assertEqual(self.Dev.get_live_data()['config']['icon_size'], 140)   # the Tracar dashboard drew icons at 1.4x
        params.set_param('elblasy_fleet_gps.icon_size', '180')
        self.assertEqual(self.Dev.get_live_data()['config']['icon_size'], 180)
        params.set_param('elblasy_fleet_gps.icon_size', '9999')
        self.assertEqual(self.Dev._icon_size(), 250)
        params.set_param('elblasy_fleet_gps.icon_size', '5')
        self.assertEqual(self.Dev._icon_size(), 50)

    def test_outsider_has_no_access(self):
        user = self.env['res.users'].with_context(no_reset_password=True).create({'name': 'N', 'login': 'nogps', 'email': 'n@example.com'})
        with self.assertRaises(AccessError):
            self.Dev.with_user(user).get_live_data()

    def test_notification_is_coalesced_and_data_free(self):
        gps_device._last_notify.clear()
        bus = self.env['bus.bus'].sudo()
        with patch.object(type(bus), '_sendone') as send:
            self.Dev._notify_live()
            self.Dev._notify_live()
            self.Dev._notify_live()
        self.assertEqual(send.call_count, 1)
        target, kind, payload = send.call_args.args
        self.assertEqual((target, kind, payload), (gps_device.BUS_CHANNEL, gps_device.BUS_TYPE, {}))


@tagged('post_install', '-at_install')
class TestMapUI(HttpCase):

    def test_map_renders_vehicles(self):
        server = self.env['fleet.gps.server'].create({'name': 'T', 'base_url': 'http://traccar.test'})
        brand = self.env['fleet.vehicle.model.brand'].create({'name': 'B'})
        model = self.env['fleet.vehicle.model'].create({'name': 'M', 'brand_id': brand.id})
        for i, (plate, speed, cat) in enumerate([('MOVE 1', 40, 'truck'), ('PARK 2', 0, 'container'), ('CAR 3', 100, 'car')], 1):
            veh = self.env['fleet.vehicle'].create({'model_id': model.id, 'license_plate': plate})
            self.env['fleet.gps.device'].create({'name': plate, 'server_id': server.id, 'traccar_id': i,
                                                 'vehicle_id': veh.id, 'status': 'online', 'category': cat})
            now = fields.Datetime.now()
            self.env['fleet.gps.position']._ingest(server, [{
                'id': i, 'deviceId': i, 'fixTime': now.isoformat() + 'Z', 'serverTime': now.isoformat() + 'Z',
                'latitude': 30.0 + i / 100, 'longitude': 31.0 + i / 100, 'speed': speed / 1.852, 'course': 45 * i, 'valid': True,
                'attributes': {'ignition': speed > 0, 'batteryLevel': 77}}], {})
        self.env.cr.flush()
        self.start_tour('/odoo/action-elblasy_fleet_gps.gps_map_action', 'elblasy_fleet_gps_map_tour', login='admin')


PUT = 'odoo.addons.elblasy_fleet_gps.models.traccar_client.TraccarClient.put'
GET = 'odoo.addons.elblasy_fleet_gps.models.traccar_client.TraccarClient.get'


@tagged('post_install', '-at_install')
class TestGeofenceAndSync(Common):

    def test_geofence_import_updates_and_archives(self):
        Fence = self.env['fleet.gps.geofence']
        with patch(GET, return_value=[{'id': 1, 'name': 'Yard', 'area': 'CIRCLE (30 31, 100)'}, {'id': 2, 'name': 'Port', 'area': 'POLYGON ((1 1, 1 2, 2 2))'}]):
            Fence._import_from_server(self.server)
        self.assertEqual(len(Fence.get_geofences()), 2)
        with patch(GET, return_value=[{'id': 1, 'name': 'Yard 2', 'area': 'CIRCLE (30 31, 200)'}]):
            Fence._import_from_server(self.server)
        self.assertEqual([g['name'] for g in Fence.get_geofences()], ['Yard 2'])  # #2 deleted in Traccar -> archived

    def test_subscription_dates_and_state(self):
        dev = self.env['fleet.gps.device'].create({'name': 'S', 'server_id': self.server.id, 'traccar_id': 5})
        self.assertEqual(dev.subscription_state, 'none')
        today = fields.Date.context_today(dev)
        dev.subscription_end = today - timedelta(days=1)
        self.assertEqual(dev.subscription_state, 'expired')
        dev.subscription_end = today + timedelta(days=5)
        self.assertEqual(dev.subscription_state, 'soon')
        dev.subscription_end = today + timedelta(days=90)
        self.assertEqual(dev.subscription_state, 'ok')

    def test_import_reads_subscription_attributes(self):
        def fake(path, params=None):
            return {'groups': [], 'geofences': [], 'devices': [
                {'id': 1, 'name': 'D', 'uniqueId': 'u', 'status': 'online',
                 'attributes': {'activatedAt': '2025-10-11', 'expiresAt': '2026-10-18'}}]}[path]
        with patch(GET, side_effect=fake):
            self.server._call  # noqa: B018
            self.env['fleet.gps.device']._import_from_server(self.server)
        dev = self.env['fleet.gps.device'].search([('traccar_id', '=', 1)])
        self.assertEqual((str(dev.subscription_start), str(dev.subscription_end)), ('2025-10-11', '2026-10-18'))

    def test_edit_is_written_back_to_traccar(self):
        dev = self.env['fleet.gps.device'].create({'name': 'Old', 'server_id': self.server.id, 'traccar_id': 7})
        remote = {'id': 7, 'name': 'Old', 'uniqueId': 'u', 'category': 'car', 'attributes': {'keep': 1}}
        with patch(GET, return_value=dict(remote)), patch(PUT) as put:
            dev.write({'name': 'New', 'category': 'truck', 'color': '#ff0000'})
        path, payload = put.call_args.args
        self.assertEqual(path, 'devices/7')
        self.assertEqual((payload['name'], payload['category'], payload['uniqueId']), ('New', 'truck', 'u'))
        self.assertEqual(payload['attributes'], {'keep': 1, 'tracarColor': '#ff0000'})

    def test_import_does_not_write_back_and_failure_does_not_block(self):
        dev = self.env['fleet.gps.device'].create({'name': 'A', 'server_id': self.server.id, 'traccar_id': 8})
        with patch(PUT) as put:
            dev.with_context(no_traccar_sync=True).write({'name': 'B'})
        put.assert_not_called()
        with patch(GET, side_effect=TraccarError('network', 'down')):
            dev.write({'name': 'C'})  # must not raise
        self.assertEqual(dev.name, 'C')
        self.assertIn('Could not update Traccar', dev.message_ids[:1].body.striptags() if hasattr(dev.message_ids[:1].body, 'striptags') else str(dev.message_ids[:1].body))


@tagged('post_install', '-at_install')
class TestNoPushMode(Common):

    def _fake(self, latest, history=None):
        calls = []

        def get(path, params=None):
            calls.append((path, params))
            if path == 'devices':
                return [{'id': 1, 'name': 'D', 'uniqueId': 'u', 'status': 'online'}]
            if path == 'positions':
                return history if (params or {}).get('deviceId') else latest
            if path == 'reports/events':
                return []
            raise AssertionError(path)
        return get, calls

    def test_polling_never_counts_as_push(self):
        get, _calls = self._fake([_pos(1)])
        with patch(GET, side_effect=get):
            self.assertEqual(self.env['fleet.gps.server'].poll_now(), 1)
        self.assertFalse(self.server.last_push_date)
        self.assertFalse(self.env['fleet.gps.server']._push_active())
        self.assertFalse(self.env['fleet.gps.device'].get_live_data()['config']['push_active'])
        self.env['fleet.gps.position']._ingest(self.server, [_pos(2)], {})  # a real push
        self.assertTrue(self.env['fleet.gps.device'].get_live_data()['config']['push_active'])

    def test_on_demand_poll_is_throttled(self):
        get, calls = self._fake([_pos(1)])
        with patch(GET, side_effect=get):
            self.assertEqual(self.env['fleet.gps.server'].poll_now(), 1)
            n = len(calls)
            self.assertEqual(self.env['fleet.gps.server'].poll_now(), 0)  # asked again at once: nothing to do
        self.assertEqual(len(calls), n)

    def test_failed_poll_backs_off(self):
        with patch(GET, side_effect=TraccarError('network', 'down')):
            self.assertEqual(self.env['fleet.gps.server'].poll_now(), 0)
            self.assertEqual(self.env['fleet.gps.server'].poll_now(), 0)
        # the next attempt is pushed into the future: a dead Traccar is not asked again every few seconds
        self.assertGreater(self.server.last_poll_date, fields.Datetime.now() + timedelta(seconds=20))

    def test_interactive_calls_use_a_short_connect_timeout(self):
        seen = []
        original = TraccarError

        def spy(self_, *a, **k):
            seen.append(self_.env.context.get('traccar_connect_timeout'))
            return {'devices': [], 'positions': []}.get(a[0], [])
        with patch('odoo.addons.elblasy_fleet_gps.models.gps_server.GpsServer._call', spy):
            self.env['fleet.gps.server'].poll_now()
        self.assertEqual(set(seen), {2})
        self.assertIs(original, TraccarError)

    def test_cron_fills_the_gap_since_the_last_stored_position(self):
        Pos = self.env['fleet.gps.position']
        Pos._ingest(self.server, [_pos(10, fix='2026-11-07T10:00:00.000+00:00', srv='2026-11-07T10:00:01.000+00:00')], {})
        dev = self.env['fleet.gps.device'].search([('traccar_id', '=', 1)])
        latest = [_pos(20, fix='2026-11-07T10:05:00.000+00:00', srv='2026-11-07T10:05:01.000+00:00')]
        history = [_pos(i, fix=f'2026-11-07T10:0{i - 10}:00.000+00:00', srv=f'2026-11-07T10:0{i - 10}:01.000+00:00') for i in (12, 14, 16, 18)]
        get, calls = self._fake(latest, history)
        with patch(GET, side_effect=get), nullcontext(), \
                patch('odoo.addons.elblasy_fleet_gps.models.gps_server.fields.Datetime.now', return_value=fields.Datetime.to_datetime('2026-11-07 10:06:00')):
            self.env['fleet.gps.server']._cron_poll()
        self.assertEqual(Pos.search_count([('device_id', '=', dev.id)]), 6)  # 10, 12, 14, 16, 18 and the latest 20
        self.assertEqual(dev.last_position_id, 20)
        self.assertTrue(any((p or {}).get('deviceId') == 1 for _path, p in calls))

    def test_outsider_cannot_trigger_polling(self):
        user = self.env['res.users'].with_context(no_reset_password=True).create({'name': 'N2', 'login': 'nogps2', 'email': 'n2@example.com'})
        with self.assertRaises(AccessError):
            self.env['fleet.gps.server'].with_user(user).poll_now()


@tagged('post_install', '-at_install')
class TestViews(TransactionCase):

    def test_every_view_of_the_module_opens(self):
        """Opening each screen once: a view that cannot be read (e.g. in dev mode, where the arch is re-read from the
        file) breaks only when a user clicks its menu."""
        from odoo.addons.base.models.ir_ui_view import get_view_arch_from_file
        from odoo.tools import file_path
        views = self.env['ir.ui.view'].search([('model', 'like', 'fleet.gps.')])
        views |= self.env['ir.ui.view'].search([('model', '=', 'res.config.settings'), ('name', 'like', '%gps%')])
        self.assertGreater(len(views), 8)
        for view in views:
            xml_id = view.get_external_id()[view.id]
            if view.arch_fs:
                arch = get_view_arch_from_file(file_path(view.arch_fs), xml_id)
                self.assertTrue(arch, f"{xml_id}: the file arch could not be read")
            self.env[view.model].get_view(view.id, view.type)


@tagged('post_install', '-at_install')
class TestDashboardUI(HttpCase):

    def test_dashboard_renders(self):
        from datetime import datetime, timedelta, timezone
        server = self.env['fleet.gps.server'].create({'name': 'T', 'base_url': 'http://traccar.test'})
        dev = self.env['fleet.gps.device'].create({'name': 'Dash 1', 'server_id': server.id, 'traccar_id': 1, 'status': 'online'})
        t0 = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None) - timedelta(hours=2)
        pts = []
        for i in range(0, 90):
            ts = (t0 + timedelta(seconds=10 * i)).strftime('%Y-%m-%dT%H:%M:%S.000+00:00')
            pts.append({'id': i + 1, 'deviceId': 1, 'fixTime': ts, 'serverTime': ts, 'latitude': 30.0 + i * 0.0012, 'longitude': 31.0,
                        'speed': 50 / 1.852, 'course': 0, 'valid': True, 'attributes': {'ignition': True}})
        self.env['fleet.gps.position']._ingest(server, pts, {})
        daily = self.env['fleet.gps.daily']
        for back in (0, 1):  # the local day of the data, whichever side of midnight Cairo is on
            day = (t0 + timedelta(days=-back)).astimezone(daily._tz()).date() if False else (datetime.now(daily._tz()) - timedelta(days=back)).date()
            daily._compute_for(dev, day, force=True)
        self.env.cr.flush()
        self.start_tour('/odoo/action-elblasy_fleet_gps.gps_dashboard_action', 'elblasy_fleet_gps_dashboard_tour', login='admin')


@tagged('post_install', '-at_install')
class TestFreshness(Common):
    """The device list and the cron must tell the same story as Traccar between two map refreshes."""

    def _dev(self, **vals):
        return self.env['fleet.gps.device'].create({'name': 'F', 'server_id': self.server.id, 'traccar_id': 1, **vals})

    def test_live_state_follows_the_map_rule(self):
        now = fields.Datetime.now()
        base = {'status': 'online', 'last_fix_time': now, 'last_latitude': 30.0, 'last_longitude': 31.0}
        self.assertEqual(self._dev(**base, last_speed=40).live_state, 'moving')
        self.assertEqual(self._dev(**base, last_speed=120, traccar_id=2).live_state, 'over')
        self.assertEqual(self._dev(**base, last_speed=0, last_ignition=True, traccar_id=3).live_state, 'idle')
        self.assertEqual(self._dev(**base, last_speed=0, traccar_id=4).live_state, 'stopped')
        self.assertEqual(self._dev(**{**base, 'status': 'offline'}, last_speed=40, traccar_id=5).live_state, 'offline')
        self.assertEqual(self._dev(status='online', traccar_id=6).live_state, 'nofix')
        stale = self._dev(**{**base, 'last_fix_time': now - timedelta(minutes=9)}, last_speed=60, traccar_id=7)
        self.assertEqual(stale.live_state, 'stopped')  # a nine-minute-old fix is not "moving now"
        self.assertEqual(stale.last_seen_ago, '9 min')

    def _fake(self, calls):
        def get(path, params=None):
            calls.append((path, (params or {}).get('deviceId')))
            return [{'id': 1, 'name': 'F', 'uniqueId': 'u', 'status': 'online'}] if path == 'devices' else []
        return get

    def test_cron_polls_every_run_without_push_but_gap_fills_every_five_minutes(self):
        self._dev(status='online')
        calls = []
        Server = self.env['fleet.gps.server']
        with patch(GET, side_effect=self._fake(calls)), nullcontext():
            Server._cron_poll()
            first = len(calls)
            self.assertTrue(self.server.last_gapfill_date)
            self.server.last_poll_date = fields.Datetime.now() - timedelta(minutes=1)
            Server._cron_poll()  # a minute later: polled again (no push), no new gap fill
        self.assertGreater(len(calls), first)
        self.assertIn('reports/events', [c[0] for c in calls[:first]])      # first run: gap fill + events
        self.assertNotIn('reports/events', [c[0] for c in calls[first:]])   # second run: light poll only

    def test_cron_leaves_a_pushing_server_alone_until_the_safety_net_is_due(self):
        self.server.write({'last_push_date': fields.Datetime.now(), 'last_poll_date': fields.Datetime.now() - timedelta(minutes=2)})
        calls = []
        with patch(GET, side_effect=self._fake(calls)), nullcontext():
            self.env['fleet.gps.server']._cron_poll()
            self.assertEqual(calls, [])  # push is healthy and the last poll was 2 min ago
            self.server.last_poll_date = fields.Datetime.now() - timedelta(minutes=6)
            self.env['fleet.gps.server']._cron_poll()
        self.assertTrue(calls)  # the safety net is due again


@tagged('post_install', '-at_install')
class TestStylesCompile(TransactionCase):
    """libsass errors never fail a page or a test: Odoo silently serves the previous CSS with a red banner. This does."""

    def test_the_module_scss_compiles_in_every_variant(self):
        for bundle, rtl in (('web.assets_backend', False), ('web.assets_backend', True), ('web.assets_web_dark', False)):
            asset = self.env['ir.qweb']._get_asset_bundle(bundle, js=False, css=True, debug_assets=False, rtl=rtl)
            asset.preprocess_css()
            self.assertFalse(asset.css_errors, f"{bundle} (rtl={rtl}): {asset.css_errors[:1]}")

    def test_vehicle_markers_and_map_css_are_not_mirrored_in_rtl(self):
        """In Arabic, rtlcss mirrored `left` / `margin-left` of the marker geometry and of MapLibre itself, which moved every
        vehicle off its place (they were drawn outside the visible map). Needs the rtlcss binary, as Odoo itself does."""
        from odoo.tools.misc import find_in_path
        if not find_in_path('rtlcss'):
            self.skipTest("rtlcss is not installed: Odoo does not mirror anything here either")
        lib = self.env['ir.qweb']._get_asset_bundle('elblasy_fleet_gps.map_lib', js=False, css=True, debug_assets=False, rtl=True)
        lib.preprocess_css()
        css = lib.css()[0].raw.decode()
        self.assertRegex(css, r"\.maplibregl-marker\{[^}]*left:0", "MapLibre markers anchor on the left, in both directions")
        self.assertNotRegex(css, r"\.maplibregl-marker\{[^}]*right:0")
        web = self.env['ir.qweb']._get_asset_bundle('web.assets_backend', js=False, css=True, debug_assets=False, rtl=True)
        web.preprocess_css()
        css = web.css()[0].raw.decode()
        badge = css[css.index('.o_gps_veh .o_gps_badge'):][:400]
        self.assertIn('margin: -1.05rem 0 0 -1.05rem', badge)      # not flipped to "-1.05rem -1.05rem 0 0"
        label = css[css.index('.o_gps_veh .o_gps_lbl'):][:300]
        self.assertIn('left: 0', label)
        self.assertIn('translateX(-50%)', label)
        # the layout itself IS mirrored: the list panel sits at the inline start (right in Arabic)
        self.assertIn('margin-right: auto', css) if '.ms-auto' in css else None
