# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Minimal HTTP helper for Peppol access-point adapter modules.

Standard library only (urllib), so transport adapters add no
third-party dependency. Every failure becomes an AccessPointError,
which the outbound send action and the inbound poll cron already catch.

Adapter test suites monkeypatch request so CI never touches a real
access point, SMP, or AS4 gateway.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from .access_point_registry import AccessPointError


def request(method, url, headers=None, body=None, form=None,
            expect_json=True, timeout=30):
    """Perform an HTTP request.

    :param expect_json: when True, decode and return parsed JSON; when
        False, return the raw response bytes (for SMP XML / receipts).
    :raises AccessPointError: on any connection, HTTP, or decode error.
    """
    req_headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        req_headers.setdefault('Content-Type', 'application/json')
    elif form is not None:
        data = urllib.parse.urlencode(form).encode('utf-8')
        req_headers.setdefault(
            'Content-Type', 'application/x-www-form-urlencoded')
    request_obj = urllib.request.Request(
        url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(request_obj, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = exc.read().decode('utf-8')[:500]
        except Exception:  # noqa: BLE001 - best-effort detail only
            detail = ''
        raise AccessPointError(
            "Access point returned HTTP %s: %s" % (exc.code, detail))
    except urllib.error.URLError as exc:
        raise AccessPointError(
            "Could not reach access point %s: %s" % (url, exc.reason))
    except Exception as exc:  # noqa: BLE001 - never leak a raw error
        raise AccessPointError("Access-point request failed: %s" % exc)
    if not expect_json:
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw.decode('utf-8'))
    except (TypeError, ValueError) as exc:
        raise AccessPointError(
            "Access point returned non-JSON response: %s" % exc)
