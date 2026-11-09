import math
import re

from odoo import api, fields, models

R_EARTH = 6371008.8


def _pairs(text):
    return [tuple(map(float, p.split())) for p in re.sub(r'[()]', '', text).split(',') if len(p.split()) >= 2]


class GpsGeofence(models.Model):
    _name = 'fleet.gps.geofence'
    _description = "GPS Geofence"
    _order = 'name'

    name = fields.Char(required=True)
    description = fields.Char()
    active = fields.Boolean(default=True)
    server_id = fields.Many2one('fleet.gps.server', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='server_id.company_id', store=True, index=True)
    traccar_id = fields.Integer("Traccar ID", required=True, index=True, copy=False)
    area = fields.Text("Area (WKT)", help="CIRCLE (lat lon, radius) / POLYGON ((lat lon, ...)) / LINESTRING (lat lon, ...)")

    _sql_constraints = [('server_traccar_uniq', 'unique(server_id, traccar_id)', "This Traccar geofence already exists.")]

    @api.model
    def _import_from_server(self, server):
        raw = server._call('geofences')
        existing = {g.traccar_id: g for g in self.with_context(active_test=False).search([('server_id', '=', server.id)])}
        seen = set()
        for g in raw:
            vals = {'name': g.get('name') or str(g['id']), 'description': g.get('description') or False, 'area': g.get('area') or False}
            seen.add(g['id'])
            if g['id'] in existing:
                existing[g['id']].write({**vals, 'active': True})
            else:
                self.create({**vals, 'server_id': server.id, 'traccar_id': g['id']})
        # deleted in Traccar -> archived here
        for tid, rec in existing.items():
            if tid not in seen and rec.active:
                rec.active = False
        return len(raw)

    @api.model
    def get_geofences(self):
        """For the live map: the area strings are parsed in the browser."""
        return [{'id': g.id, 'name': g.name, 'area': g.area} for g in self.search([('area', '!=', False)])]

    def _contains(self, lat, lon):
        """True when the point is inside this fence. Traccar areas are WKT with "lat lon" order."""
        self.ensure_one()
        m = re.match(r'^\s*(\w+)\s*\(([\s\S]*)\)\s*$', self.area or '')
        if not m:
            return False
        kind = m.group(1).upper()
        try:
            if kind == 'CIRCLE':
                centre, radius = m.group(2).split(',')
                c_lat, c_lon = map(float, centre.split())
                r = math.pi / 180
                x = math.sin((lat - c_lat) * r / 2) ** 2 + math.cos(lat * r) * math.cos(c_lat * r) * math.sin((lon - c_lon) * r / 2) ** 2
                return 2 * R_EARTH * math.asin(math.sqrt(x)) <= float(radius)
            if kind == 'POLYGON':
                pts = _pairs(m.group(2))
                inside = False
                for (a_lat, a_lon), (b_lat, b_lon) in zip(pts, pts[1:] + pts[:1]):
                    if (a_lon > lon) != (b_lon > lon) and lat < (b_lat - a_lat) * (lon - a_lon) / (b_lon - a_lon) + a_lat:
                        inside = not inside
                return inside
        except (ValueError, ZeroDivisionError):
            return False
        return False  # a LINESTRING has no inside
