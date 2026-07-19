#!/usr/bin/env python3

"""
http_retry
Thin wrapper around requests that adds exponential backoff for transient
failures (429, 5xx, connection errors). Drop-in for requests.get/post/put/delete.
"""

import time

import requests

from ultrasonics import logs

log = logs.create_log(__name__)

_SESSION = requests.Session()

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4
_BASE_DELAY = 2.0   # seconds; doubled each retry


def _request(method, url, **kwargs):
    attempts = 0
    delay = _BASE_DELAY
    while True:
        attempts += 1
        try:
            resp = _SESSION.request(method, url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempts >= _MAX_ATTEMPTS:
                raise
            log.warning(f"http_retry: {method} {url} connection error ({exc}), retrying in {delay}s")
            time.sleep(delay)
            delay *= 2
            continue

        if resp.status_code not in _RETRY_STATUSES or attempts >= _MAX_ATTEMPTS:
            return resp

        retry_after = resp.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else delay
        log.warning(f"http_retry: {method} {url} → {resp.status_code}, waiting {wait:.0f}s (attempt {attempts})")
        time.sleep(wait)
        delay *= 2


def get(url, **kwargs):
    kwargs.setdefault("timeout", 30)
    return _request("GET", url, **kwargs)


def post(url, **kwargs):
    kwargs.setdefault("timeout", 30)
    return _request("POST", url, **kwargs)


def put(url, **kwargs):
    kwargs.setdefault("timeout", 30)
    return _request("PUT", url, **kwargs)


def delete(url, **kwargs):
    kwargs.setdefault("timeout", 30)
    return _request("DELETE", url, **kwargs)
