{
    'name': 'Elblasy Fleet GPS (Traccar)',
    'version': '17.0.1.3.0',
    'author': 'Elblasy.app',
    'website': 'https://elblasy.app',
    'category': 'Human Resources/Fleet',
    'summary': "Live GPS tracking, trips and reports for Fleet vehicles, powered by your own Traccar server",
    'description': """
Elblasy Fleet GPS
=================
Connects Odoo Fleet to a Traccar server: import devices, link each one to a vehicle,
receive positions by push (Traccar forward.url) with a polling cron as a safety net.
""",
    'depends': ['base', 'bus', 'mail', 'fleet'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'views/gps_server_views.xml',
        'views/gps_device_views.xml',
        'views/fleet_vehicle_views.xml',
        'views/res_config_settings_views.xml',
        'views/menus.xml',
        'views/report_views.xml',
        'views/event_views.xml',
        'views/command_views.xml',
        'data/ir_cron.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'elblasy_fleet_gps/static/src/map/*',
            'elblasy_fleet_gps/static/src/dashboard/*',
            'elblasy_fleet_gps/static/src/playback/*',
            ('remove', 'elblasy_fleet_gps/static/src/map/*.dark.scss'),
            ('remove', 'elblasy_fleet_gps/static/src/dashboard/*.dark.scss'),
            ('remove', 'elblasy_fleet_gps/static/src/playback/*.dark.scss'),
        ],
        'web.assets_web_dark': [
            'elblasy_fleet_gps/static/src/map/*.dark.scss',
            'elblasy_fleet_gps/static/src/dashboard/*.dark.scss',
            'elblasy_fleet_gps/static/src/playback/*.dark.scss',
        ],
        # heavy (1 MB): only downloaded when the live map is opened, never for the rest of the backend
        'elblasy_fleet_gps.map_lib': [
            'elblasy_fleet_gps/static/lib/maplibre/maplibre-gl.js',
            'elblasy_fleet_gps/static/lib/maplibre/maplibre-gl.css',
        ],
    },
    'icon': '/elblasy_fleet_gps/static/description/icon.png',
    'license': 'LGPL-3',
    'installable': True,
    'application': True,
}
