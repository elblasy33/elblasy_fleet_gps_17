from contextlib import nullcontext
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import HttpCase, tagged

from .test_gps import Common, _pos

GET = 'odoo.addons.elblasy_fleet_gps.models.traccar_client.TraccarClient.get'


def _ev(eid, etype='ignitionOn', dev=1, when='2026-11-01T10:00:00.000+00:00', **extra):
    return {'id': eid, 'type': etype, 'deviceId': dev, 'eventTime': when, 'positionId': extra.pop('positionId', 0),
            'geofenceId': extra.pop('geofenceId', 0), 'attributes': extra.pop('attributes', {}), **extra}


@tagged('post_install', '-at_install')
class TestEvents(Common):

    def setUp(self):
        super().setUp()
        self.dev = self.env['fleet.gps.device'].create({'name': 'D', 'server_id': self.server.id, 'traccar_id': 1})
        self.Event = self.env['fleet.gps.event']

    def test_ingest_stores_dedupes_and_maps_unknown_types(self):
        n = self.Event._ingest(self.server, [_ev(1), _ev(2, 'deviceOverspeed'), _ev(3, 'someNewType')])
        self.assertEqual(n, 3)
        self.assertEqual(self.Event._ingest(self.server, [_ev(1), _ev(4, 'ignitionOff')]), 1)  # #1 is a duplicate
        types = {e.traccar_id: (e.type, e.type_raw) for e in self.Event.search([])}
        self.assertEqual(types[3], ('other', 'someNewType'))
        self.assertEqual(self.server.last_event_time, fields.Datetime.to_datetime('2026-11-01 10:00:00'))

    def test_position_and_geofence_are_resolved(self):
        self.env['fleet.gps.position']._ingest(self.server, [_pos(77)], {})
        fence = self.env['fleet.gps.geofence'].create({'name': 'Yard', 'server_id': self.server.id, 'traccar_id': 5})
        self.Event._ingest(self.server, [_ev(1, 'geofenceEnter', positionId=77, geofenceId=5)])
        e = self.Event.search([])
        self.assertEqual((e.latitude, e.longitude, e.geofence_id), (30.0, 31.0, fence))
        self.Event._ingest(self.server, [_ev(2, 'geofenceExit', positionId=999, geofenceId=5)],
                           positions={999: {'id': 999, 'latitude': 29.5, 'longitude': 30.5}})  # from a push payload
        self.assertEqual(self.Event.search([('traccar_id', '=', 2)]).latitude, 29.5)

    def test_unknown_device_and_garbage_are_ignored(self):
        self.assertEqual(self.Event._ingest(self.server, [_ev(1, dev=999), {'id': 2}, 'x', _ev(3, when='garbage')]), 0)
        self.assertFalse(self.env['fleet.gps.device'].search([('traccar_id', '=', 999)]))

    def test_alarm_posts_a_note_on_the_device(self):
        self.Event._ingest(self.server, [_ev(1, 'alarm', attributes={'alarm': 'sos'}), _ev(2, 'alarm', attributes={'alarm': 'general'})])
        notes = [m.body for m in self.dev.message_ids if 'Alarm' in str(m.body)]
        self.assertEqual(len(notes), 1)  # only the serious kinds are worth a chatter message

    def test_alarm_notes_are_throttled_and_vibration_is_silent(self):
        flood = [_ev(i, 'alarm', when=f'2026-11-01T10:{i % 60:02d}:00.000+00:00', attributes={'alarm': 'vibration'}) for i in range(1, 40)]
        flood += [_ev(100 + i, 'alarm', when=f'2026-11-01T10:{i * 5:02d}:00.000+00:00', attributes={'alarm': 'powerCut'}) for i in range(0, 8)]
        self.Event._ingest(self.server, flood)
        self.assertEqual(self.Event.search_count([('type', '=', 'alarm')]), 47)  # every event is stored...
        notes = [m for m in self.dev.message_ids if 'Alarm' in str(m.body)]
        self.assertEqual(len(notes), 1)  # ...but powerCut every 5 min for 35 min is one note, vibration none
        self.Event._ingest(self.server, [_ev(900, 'alarm', when='2026-11-01T12:00:00.000+00:00', attributes={'alarm': 'powerCut'})])
        self.assertEqual(len([m for m in self.dev.message_ids if 'Alarm' in str(m.body)]), 2)  # an hour later: a new note

    def test_poll_cron_fetches_events_since_the_last_one(self):
        self.Event._ingest(self.server, [_ev(1, when='2026-11-07 09:00:00')])
        calls = []

        def get(path, params=None):
            calls.append((path, params))
            if path == 'devices':
                return [{'id': 1, 'name': 'D', 'uniqueId': 'u', 'status': 'online'}]
            if path == 'positions':
                return []
            return [_ev(1, when='2026-11-07T09:00:00.000+00:00'), _ev(2, 'deviceMoving', when='2026-11-07T09:30:00.000+00:00')]
        with patch(GET, side_effect=get), nullcontext():
            self.env['fleet.gps.server']._cron_poll()
        path, params = next(c for c in calls if c[0] == 'reports/events')
        self.assertEqual(params['deviceId'], [1])
        self.assertTrue(params['from'].startswith('2026-11-07T08:59'))  # one minute before the newest we hold
        self.assertEqual(self.Event.search_count([]), 2)

    def test_mark_seen_and_purge(self):
        self.Event._ingest(self.server, [_ev(1), _ev(2, when='2020-01-01T00:00:00.000+00:00')])
        self.Event.search([('traccar_id', '=', 1)]).action_mark_seen()
        self.assertEqual(self.Event.search_count([('seen', '=', False)]), 1)
        self.Event._cron_purge()
        self.assertEqual(self.Event.search([]).mapped('traccar_id'), [1])


@tagged('post_install', '-at_install')
class TestSubscriptionAlerts(Common):

    def test_one_alert_per_end_date(self):
        today = fields.Date.context_today(self.env.user)
        soon = self.env['fleet.gps.device'].create({'name': 'Soon', 'server_id': self.server.id, 'traccar_id': 1,
                                                     'subscription_end': today + timedelta(days=5)})
        old = self.env['fleet.gps.device'].create({'name': 'Old', 'server_id': self.server.id, 'traccar_id': 2,
                                                    'subscription_end': today - timedelta(days=30)})
        far = self.env['fleet.gps.device'].create({'name': 'Far', 'server_id': self.server.id, 'traccar_id': 3,
                                                    'subscription_end': today + timedelta(days=200)})
        Dev = self.env['fleet.gps.device']
        Dev._cron_subscription_alerts()
        self.assertEqual(len(soon.activity_ids), 1)
        self.assertEqual(len(old.activity_ids), 1)
        self.assertIn('expired', old.activity_ids.summary)
        self.assertFalse(far.activity_ids)
        Dev._cron_subscription_alerts()  # a second run must not duplicate
        self.assertEqual((len(soon.activity_ids), len(old.activity_ids)), (1, 1))
        soon.subscription_end = today + timedelta(days=365)  # renewed...
        soon.subscription_end = today + timedelta(days=3)    # ...and ends again: a new alert is justified
        Dev._cron_subscription_alerts()
        self.assertEqual(len(soon.activity_ids), 2)


@tagged('post_install', '-at_install')
class TestEventEndpoint(HttpCase):

    def test_event_push(self):
        server = self.env['fleet.gps.server'].create({'name': 'T', 'base_url': 'http://traccar.test'})
        self.env['fleet.gps.device'].create({'name': 'D', 'server_id': server.id, 'traccar_id': 1})
        token = server.sudo().push_token
        self.env.cr.flush()
        body = {'event': _ev(10, 'alarm', attributes={'alarm': 'powerCut'}), 'position': {'id': 5, 'latitude': 30.2, 'longitude': 31.2}}
        r = self.url_open(f'/fleet_gps/event/{token}', data=json.dumps(body), headers={'Content-Type': 'application/json'})
        self.assertEqual((r.status_code, r.json()['stored']), (200, 1))
        ev = self.env['fleet.gps.event'].search([])
        self.assertEqual((ev.type, ev.alarm), ('alarm', 'powerCut'))
        self.assertEqual(self.url_open('/fleet_gps/event/nope', data='{}').status_code, 404)
