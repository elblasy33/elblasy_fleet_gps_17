"""Thin HTTP layer for the Traccar REST API (no ORM access, easy to mock)."""
import threading
from datetime import datetime, timezone

import requests


def commit_progress(env, done=1):
    """Odoo 17 has no cron progress API: commit what was done so a long job keeps its work if it is killed
    (never inside a test, which runs in one transaction). Returns True to keep going."""
    if not getattr(threading.current_thread(), 'testing', False):
        env.cr.commit()
    return True


class TraccarError(Exception):
    """``kind`` is one of: network, auth, http, response."""

    def __init__(self, kind, message, status_code=None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code


class TraccarClient:

    def __init__(self, base_url, token=None, user=None, password=None, timeout=(5, 30), verify_ssl=True):
        self.base_url = (base_url or '').rstrip('/')
        self._token = token or ''
        self._basic = (user, password) if user and password else None
        self.timeout = timeout
        self.verify = bool(verify_ssl)
        self._http = requests.Session()

    def get(self, path, params=None):
        return self._request('GET', path, params=params)

    def put(self, path, payload):
        return self._request('PUT', path, json=payload)

    def post(self, path, payload):
        return self._request('POST', path, json=payload)

    def _request(self, method, path, params=None, json=None):
        if not self.base_url:
            raise TraccarError('network', "Traccar URL is not configured.")
        headers = {'Accept': 'application/json'}
        auth = None
        if self._token:
            headers['Authorization'] = f'Bearer {self._token}'
        elif self._basic:
            auth = self._basic
        try:
            resp = self._http.request(method, f'{self.base_url}/api/{path.lstrip("/")}', params=params, json=json,
                                      headers=headers, auth=auth, timeout=self.timeout, verify=self.verify)
        except requests.exceptions.SSLError:
            raise TraccarError('network', "TLS certificate verification failed.")
        except requests.exceptions.Timeout:
            raise TraccarError('network', "The Traccar server did not answer in time.")
        except requests.exceptions.RequestException:
            raise TraccarError('network', "The Traccar server could not be reached.")  # never echo URL/headers
        if resp.status_code in (401, 403):
            raise TraccarError('auth', "Traccar rejected the credentials.", resp.status_code)
        if resp.status_code >= 400:
            raise TraccarError('http', f"Traccar answered with HTTP {resp.status_code}.", resp.status_code)
        if not resp.content:
            return {}  # e.g. 202 Accepted for a queued command
        try:
            return resp.json()
        except ValueError:
            raise TraccarError('response', "Traccar returned an invalid response.")


def parse_time(value):
    """Traccar ISO-8601 (...Z / +00:00) -> naive UTC datetime, or False."""
    if not value or not isinstance(value, str):
        return False
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return False
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(microsecond=0)
