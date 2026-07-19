#!/usr/bin/env python3

"""
notifications
Sends run-completion notifications via webhook.
Reads the notification_url from global settings (ultrasonics DB).
If not configured, notifications are silently skipped.
"""

import json

import requests

from ultrasonics import database, logs

log = logs.create_log(__name__)


def send_run_notification(applet_id, success, summary=None):
    """Send a POST webhook notification on applet completion."""
    url = database.Core().get("notification_url")
    if not url:
        return

    payload = {
        "event": "applet_run_complete",
        "applet_id": applet_id,
        "success": success,
        "summary": summary or "",
    }

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code >= 400:
            log.warning(f"Notification webhook returned {resp.status_code}")
        else:
            log.info(f"Notification sent for applet {applet_id}")
    except requests.RequestException as e:
        log.warning(f"Notification failed: {e}")
