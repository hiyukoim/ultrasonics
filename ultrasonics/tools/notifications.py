#!/usr/bin/env python3

"""
notifications
Sends run-completion notifications via webhook and/or email.
Channels are configured in global settings (ultrasonics DB).
Silently skips if not configured.

Notification events:
  BATCH_END              - applet run completed (success or failure)
  TASK_ERROR             - applet run failed
  PLATFORM_DISCONNECTED  - a platform connection token has expired/failed
"""

import json
import smtplib
import ssl
from email.mime.text import MIMEText

import requests

from ultrasonics import database, logs

log = logs.create_log(__name__)


def _get_settings():
    db = database.Core()
    return {
        "webhook_url": db.get("notification_url") or "",
        "smtp_host": db.get("smtp_host") or "",
        "smtp_port": db.get("smtp_port") or "587",
        "smtp_user": db.get("smtp_user") or "",
        "smtp_pass": db.get("smtp_pass") or "",
        "smtp_from": db.get("smtp_from") or "",
        "smtp_to": db.get("smtp_to") or "",
        "notifications": db.get("notifications") or "BATCH_END,TASK_ERROR,PLATFORM_DISCONNECTED",
    }


def _subscribed(event, settings):
    subscribed = [e.strip().upper() for e in settings["notifications"].split(",")]
    return event.upper() in subscribed


def _send_webhook(url, payload):
    if not url:
        return
    try:
        resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
        if resp.status_code >= 400:
            log.warning(f"Notification webhook returned {resp.status_code}")
    except requests.RequestException as e:
        log.warning(f"Webhook notification failed: {e}")


def _send_email(settings, subject, body):
    host = settings["smtp_host"]
    if not host:
        return
    try:
        port = int(settings["smtp_port"] or 587)
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = settings["smtp_from"] or settings["smtp_user"]
        msg["To"] = settings["smtp_to"]

        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx) as server:
                if settings["smtp_user"]:
                    server.login(settings["smtp_user"], settings["smtp_pass"])
                server.sendmail(msg["From"], settings["smtp_to"].split(","), msg.as_string())
        else:
            with smtplib.SMTP(host, port) as server:
                server.ehlo()
                server.starttls(context=ctx)
                if settings["smtp_user"]:
                    server.login(settings["smtp_user"], settings["smtp_pass"])
                server.sendmail(msg["From"], settings["smtp_to"].split(","), msg.as_string())
        log.info(f"Email notification sent: {subject}")
    except Exception as e:
        log.warning(f"Email notification failed: {e}")


def send_run_notification(applet_id, success, summary=None):
    """BATCH_END (always) + TASK_ERROR (on failure)."""
    settings = _get_settings()
    event = "TASK_ERROR" if not success else "BATCH_END"

    payload = {
        "event": event,
        "applet_id": applet_id,
        "success": success,
        "summary": summary or "",
    }

    if _subscribed("BATCH_END", settings):
        _send_webhook(settings["webhook_url"], payload)
        subject = f"ultrasonics: applet {'completed' if success else 'failed'}"
        _send_email(settings, subject, summary or event)

    if not success and _subscribed("TASK_ERROR", settings):
        # Already sent above if BATCH_END was also subscribed; avoid double send
        if not _subscribed("BATCH_END", settings):
            _send_webhook(settings["webhook_url"], payload)
            _send_email(settings, f"ultrasonics: TASK_ERROR — {applet_id}", summary or "")


def send_platform_disconnected(platform, reason=""):
    """PLATFORM_DISCONNECTED — a platform token has expired or failed."""
    settings = _get_settings()
    if not _subscribed("PLATFORM_DISCONNECTED", settings):
        return

    payload = {
        "event": "PLATFORM_DISCONNECTED",
        "platform": platform,
        "reason": reason,
    }
    _send_webhook(settings["webhook_url"], payload)
    _send_email(
        settings,
        f"ultrasonics: platform disconnected — {platform}",
        f"Platform {platform} has been disconnected.\n{reason}",
    )
    log.warning(f"PLATFORM_DISCONNECTED notification sent for {platform}")
