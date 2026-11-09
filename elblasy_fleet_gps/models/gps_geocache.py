import logging

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)
URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "Odoo-Fleet-GPS/1.0 (elblasy_fleet_gps)"
LOCK_KEY = 7766001  # advisory lock: Nominatim allows one request a second


class GpsGeocache(models.Model):
    _name = 'fleet.gps.geocache'
    _description = "Reverse Geocoding Cache"

    key = fields.Char(required=True, index=True)
    address = fields.Char()

    _sql_constraints = [('key_uniq', 'unique(key)', "Duplicate cache key.")]

    @api.model
    def _lookup(self, lat, lon):
        """Street-level address of a point, or False. Opt-in (Settings): it sends coordinates to the public OpenStreetMap
        Nominatim service. Cells of about 100 m share one lookup and every answer is kept."""
        if not (lat or lon) or not (self.env['ir.config_parameter'].sudo().get_param('elblasy_fleet_gps.geocoding') in ('True', 'true', '1')):
            return False
        lang = 'ar' if (self.env.user.lang or '').startswith('ar') else 'en'
        key = f"{lang}|{lat:.3f}|{lon:.3f}"
        hit = self.sudo().search([('key', '=', key)], limit=1)
        if hit:
            return hit.address or False
        self.env.cr.execute("SELECT pg_try_advisory_xact_lock(%s)", (LOCK_KEY,))
        if not self.env.cr.fetchone()[0]:
            return False  # another worker is asking right now: try again on the next card refresh
        try:
            r = requests.get(URL, params={'format': 'jsonv2', 'zoom': 17, 'addressdetails': 1, 'lat': round(lat, 3), 'lon': round(lon, 3),
                                          'accept-language': lang}, headers={'User-Agent': USER_AGENT}, timeout=5)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            return False
        a = data.get('address') or {}
        parts = [a.get('road') or a.get('pedestrian') or a.get('industrial'), a.get('neighbourhood') or a.get('suburb') or a.get('quarter'),
                 a.get('city') or a.get('town') or a.get('village') or a.get('county') or a.get('state')]
        parts = [p for p in parts if p] or str(data.get('display_name') or '').split(',')[:3]
        address = ('، ' if lang == 'ar' else ', ').join(p.strip() for p in parts)
        self.sudo().create({'key': key, 'address': address})
        return address or False
