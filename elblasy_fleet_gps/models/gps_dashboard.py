from datetime import datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

MAX_DAYS = 93
FRESH = timedelta(minutes=5)   # same rule as the live map: an old fix is not "moving now"
LOW_BATTERY = 20
TOP = 10


class GpsDashboard(models.AbstractModel):
    _name = 'fleet.gps.dashboard'
    _description = "GPS Dashboard"

    @api.model
    def _state_colors(self):
        params = self.env['ir.config_parameter'].sudo()
        return {k: (params.get_param(f'elblasy_fleet_gps.color_{k}') or '') or default for k, default in (
            ('moving', '#23e76a'), ('idle', '#f59e0b'), ('stopped', '#27aef1'), ('over', '#f20202'), ('offline', '#727374'))}

    @api.model
    def get_data(self, date_from, date_to, device_ids=None):
        """Everything the dashboard shows for the local days ``date_from``..``date_to`` (inclusive), computed from the
        daily summaries (small: one row per device and day) and the live state of the devices."""
        d_from, d_to = fields.Date.to_date(date_from), fields.Date.to_date(date_to)
        if not d_from or not d_to or d_to < d_from or (d_to - d_from).days > MAX_DAYS:
            raise UserError(_("Choose a range of at most %s days.", MAX_DAYS))
        Device = self.env['fleet.gps.device']
        all_devices = Device.search([('disabled', '=', False)], order='name')
        devices = all_devices.filtered(lambda d: d.id in device_ids) if device_ids else all_devices
        domain = [('date', '>=', d_from), ('date', '<=', d_to), ('device_id', 'in', devices.ids)]
        rows = self.env['fleet.gps.daily'].search_read(domain, [
            'device_id', 'date', 'distance_km', 'moving_hours', 'idle_hours', 'stopped_hours', 'max_speed', 'avg_speed',
            'trip_count', 'over_count', 'harsh_brake', 'harsh_accel', 'score', 'hour_moving', 'speed_hist'])
        limit = int(self.env['ir.config_parameter'].sudo().get_param('elblasy_fleet_gps.speed_limit') or 0) or 80

        sum_ = lambda key: sum(r[key] for r in rows)  # noqa: E731
        moving_h = sum_('moving_hours')
        scored = [r for r in rows if r['moving_hours'] > 0]
        by_day, by_dev = {}, {}
        hist, hourly = [0.0] * 6, [0.0] * 24
        by_day_hours = {}
        for r in rows:
            by_day[r['date']] = by_day.get(r['date'], 0.0) + r['distance_km']
            dev = by_dev.setdefault(r['device_id'][0], {'name': r['device_id'][1], 'km': 0.0, 'trips': 0, 'moving': 0.0,
                                                        'idle': 0.0, 'stopped': 0.0, 'score_sum': 0.0, 'score_n': 0})
            dev['km'] += r['distance_km']
            dev['trips'] += r['trip_count']
            dev['moving'] += r['moving_hours']
            dev['idle'] += r['idle_hours']
            dev['stopped'] += r['stopped_hours']
            if r['moving_hours'] > 0:
                dev['score_sum'] += r['score']
                dev['score_n'] += 1
            for i, ms in enumerate(r['speed_hist'] or []):
                hist[i] += ms / 3.6e6
            day_hours = by_day_hours.setdefault(r['date'], [0.0] * 24)
            for i, ms in enumerate(r['hour_moving'] or []):
                hourly[i] += ms / 3.6e6
                day_hours[i] += ms / 3.6e6
        days = [d_from + timedelta(days=i) for i in range((d_to - d_from).days + 1)]
        now = fields.Datetime.now()
        live = {'moving': 0, 'stopped': 0, 'offline': 0}
        for d in devices:
            if d.status != 'online':
                live['offline'] += 1
            elif d.last_fix_time and now - d.last_fix_time < FRESH and d.last_speed > 2:
                live['moving'] += 1
            else:
                live['stopped'] += 1
        ranked = sorted(by_dev.values(), key=lambda x: -x['km'])
        events = self.env['fleet.gps.event']._read_group(
            [('event_time', '>=', datetime.combine(d_from, datetime.min.time())),
             ('event_time', '<', datetime.combine(d_to + timedelta(days=1), datetime.min.time())),
             ('device_id', 'in', devices.ids)], ['type'], ['__count']) if devices else []
        trips = self.env['fleet.gps.trip'].search_read(
            [('date', '>=', d_from), ('date', '<=', d_to), ('device_id', 'in', devices.ids)],
            ['device_id', 'start_time', 'end_time', 'distance_km', 'max_speed'], order='start_time desc', limit=8)
        sel = dict(self.env['fleet.gps.event']._fields['type']._description_selection(self.env))
        insights = self._insights(devices, rows, by_dev, d_from, d_to, limit)
        return {
            'insights': insights,
            'heatmap': self._heatmap(by_day_hours, days),
            'limit': limit,
            'colors': self._state_colors(),
            'devices': [{'id': d.id, 'name': d.name} for d in all_devices],
            'kpi': {
                'vehicles': len(devices), 'moving_now': live['moving'], 'online': live['moving'] + live['stopped'],
                'distance_km': sum_('distance_km'), 'trips': sum_('trip_count'), 'max_speed': max((r['max_speed'] for r in rows), default=0),
                'avg_speed': sum(r['avg_speed'] * r['moving_hours'] for r in rows) / moving_h if moving_h else 0,
                'moving_hours': moving_h, 'idle_hours': sum_('idle_hours'),
                'idle_pct': 100 * sum_('idle_hours') / (moving_h + sum_('idle_hours')) if moving_h + sum_('idle_hours') else 0,
                'over_count': sum_('over_count'), 'harsh_brake': sum_('harsh_brake'), 'harsh_accel': sum_('harsh_accel'),
                'score': round(sum(r['score'] for r in scored) / len(scored)) if scored else 0,
                'events': sum(n for _t, n in events),
            },
            'live': live,
            'daily': [{'date': str(d), 'km': round(by_day.get(d, 0.0), 1)} for d in days],
            'by_device': [{'name': x['name'], 'km': round(x['km'], 1), 'trips': x['trips']} for x in ranked[:TOP]],
            'speed_hist': [round(h, 2) for h in hist],
            'utilization': [{'name': x['name'], 'moving': round(x['moving'], 1), 'idle': round(x['idle'], 1), 'stopped': round(x['stopped'], 1)}
                            for x in sorted(by_dev.values(), key=lambda x: -(x['moving'] + x['idle']))[:TOP]],
            'hourly': [round(h, 2) for h in hourly],
            'scores': sorted(({'name': x['name'], 'score': round(x['score_sum'] / x['score_n'])} for x in by_dev.values() if x['score_n']),
                             key=lambda x: x['score'])[:TOP],
            'event_types': sorted(({'type': t, 'label': sel.get(t, t), 'count': n} for t, n in events), key=lambda x: -x['count']),
            'low_battery': [{'id': d.id, 'name': d.name, 'battery': d.last_battery} for d in devices
                            if d.last_battery and d.last_battery <= LOW_BATTERY],
            'trips': [{'device': t['device_id'][1], 'start': fields.Datetime.to_string(t['start_time']),
                       'end': fields.Datetime.to_string(t['end_time']), 'km': t['distance_km'], 'max': t['max_speed']} for t in trips],
            'has_data': bool(rows),
        }

    @api.model
    def _heatmap(self, by_day_hours, days):
        """Driving hours by hour of day: one row per day (up to 31 days), else one per weekday."""
        if len(days) <= 31:
            rows = [{'label': str(d), 'values': [round(v, 2) for v in by_day_hours.get(d, [0.0] * 24)]} for d in days]
            return {'mode': 'day', 'rows': rows, 'max': max((v for r in rows for v in r['values']), default=0)}
        names = [_("Mon"), _("Tue"), _("Wed"), _("Thu"), _("Fri"), _("Sat"), _("Sun")]
        acc = [[0.0] * 24 for __ in range(7)]
        for d, hours in by_day_hours.items():
            for i, v in enumerate(hours):
                acc[d.weekday()][i] += v
        rows = [{'label': names[i], 'values': [round(v, 2) for v in acc[i]]} for i in range(7)]
        return {'mode': 'weekday', 'rows': rows, 'max': max((v for r in rows for v in r['values']), default=0)}

    @api.model
    def _insights(self, devices, rows, by_dev, d_from, d_to, limit):
        """Plain-language findings, most useful first. Each is {level: good|warn|info, text}."""
        out = []
        km = sum(r['distance_km'] for r in rows)
        span = (d_to - d_from).days + 1
        prev_from, prev_to = d_from - timedelta(days=span), d_from - timedelta(days=1)
        prev = self.env['fleet.gps.daily'].search_read(
            [('date', '>=', prev_from), ('date', '<=', prev_to), ('device_id', 'in', devices.ids)], ['distance_km', 'over_count'])
        prev_km, prev_over = sum(r['distance_km'] for r in prev), sum(r['over_count'] for r in prev)
        if prev_km > 0 and km > 0:
            pct = round(100 * (km - prev_km) / prev_km)
            if abs(pct) >= 5:
                out.append({'level': 'info', 'text': _("Distance is %(dir)s %(pct)s%% compared with the previous %(n)s days (%(km)s km, was %(prev)s km).",
                                                       dir=_("up") if pct > 0 else _("down"), pct=abs(pct), n=span, km=round(km), prev=round(prev_km))})
        ranked = sorted(by_dev.values(), key=lambda x: -x['km'])
        if ranked and ranked[0]['km'] > 0:
            out.append({'level': 'info', 'text': _("Most driven: %(name)s with %(km)s km in %(trips)s trips.",
                                                   name=ranked[0]['name'], km=round(ranked[0]['km']), trips=ranked[0]['trips'])})
        over = {}
        harsh = {}
        for r in rows:
            over[r['device_id'][1]] = over.get(r['device_id'][1], 0) + r['over_count']
            harsh[r['device_id'][1]] = harsh.get(r['device_id'][1], 0) + r['harsh_brake'] + r['harsh_accel']
        if over and max(over.values()) > 0:
            name = max(over, key=over.get)
            extra = (_(" Previous period: %s events overall.", prev_over) if prev_over else "")
            out.append({'level': 'warn', 'text': _("%(name)s went over %(limit)s km/h %(n)s times.", name=name, limit=limit, n=over[name]) + extra})
        if harsh and max(harsh.values()) >= 3:
            name = max(harsh, key=harsh.get)
            out.append({'level': 'warn', 'text': _("%(name)s had %(n)s harsh braking or acceleration events.", name=name, n=harsh[name])})
        moving_h, idle_h = sum(r['moving_hours'] for r in rows), sum(r['idle_hours'] for r in rows)
        if moving_h + idle_h > 0 and idle_h / (moving_h + idle_h) >= 0.25:
            out.append({'level': 'warn', 'text': _("Idling is %(pct)s%% of engine time (%(h)s h): worth checking waiting at yards and queues.",
                                                   pct=round(100 * idle_h / (moving_h + idle_h)), h=round(idle_h))})
        scored = sorted((x for x in by_dev.values() if x['score_n']), key=lambda x: x['score_sum'] / x['score_n'])
        if scored and scored[0]['score_sum'] / scored[0]['score_n'] < 70:
            out.append({'level': 'warn', 'text': _("Lowest driving score: %(name)s with %(score)s/100.", name=scored[0]['name'],
                                                   score=round(scored[0]['score_sum'] / scored[0]['score_n']))})
        still = [d.name for d in devices if d.status == 'online' and d.id not in {r['device_id'][0] for r in rows if r['distance_km'] > 0}]
        if rows and still:
            out.append({'level': 'info', 'text': _("%(n)s online vehicles did not move in this period: %(names)s.", n=len(still), names=", ".join(still[:6]))})
        day_ago = fields.Datetime.now() - timedelta(hours=24)
        silent = [d.name for d in devices if d.status != 'online' and d.last_update and d.last_update < day_ago]
        if silent:
            out.append({'level': 'warn', 'text': _("%(n)s vehicles have not reported for over 24 hours: %(names)s.", n=len(silent), names=", ".join(silent[:6]))})
        expired = devices.filtered(lambda d: d.subscription_state == 'expired')
        if expired:
            out.append({'level': 'warn', 'text': _("%(n)s GPS subscriptions have expired: %(names)s.", n=len(expired), names=", ".join(expired.mapped('name')[:6]))})
        if not out and rows:
            out.append({'level': 'good', 'text': _("Nothing unusual in this period.")})
        return out[:8]
