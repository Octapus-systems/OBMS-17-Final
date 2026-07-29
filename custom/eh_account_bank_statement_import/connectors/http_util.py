# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Minimal HTTP helper for live bank-connector extension modules.

Standard library only (urllib), so connector extensions add no
third-party dependency. Every transport or decode failure becomes a
ConnectorError, which the fetch framework already catches per profile.

Connector test suites monkeypatch request_json so CI never touches a
real bank API.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from .base import ConnectorError


def request_json(method, url, headers=None, body=None, form=None, timeout=30):
    """Perform an HTTP request and return the decoded JSON response.

    :param method: 'GET' / 'POST' / etc.
    :param url: absolute URL (build any query string into it).
    :param headers: optional dict of request headers.
    :param body: optional dict sent as a JSON request body.
    :param form: optional dict sent as application/x-www-form-urlencoded.
    :returns: parsed JSON (dict/list), or {} for an empty body.
    :raises ConnectorError: on any connection, HTTP, or decode error.
    """
    req_headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        req_headers.setdefault('Content-Type', 'application/json')
    elif form is not None:
        data = urllib.parse.urlencode(form).encode('utf-8')
        req_headers.setdefault(
            'Content-Type', 'application/x-www-form-urlencoded',
        )
    request = urllib.request.Request(
        url, data=data, headers=req_headers, method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = exc.read().decode('utf-8')[:500]
        except Exception:  # noqa: BLE001 - best-effort detail only
            detail = ''
        raise ConnectorError(
            "Bank API returned HTTP %s: %s" % (exc.code, detail),
        )
    except urllib.error.URLError as exc:
        raise ConnectorError(
            "Could not reach bank API %s: %s" % (url, exc.reason),
        )
    except Exception as exc:  # noqa: BLE001 - never leak a raw error
        raise ConnectorError("Bank API request failed: %s" % exc)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ConnectorError(
            "Bank API returned non-JSON response: %s" % exc,
        )


def build_url(base, path, params=None):
    """Join base + path and append an optional query string."""
    url = base.rstrip('/') + '/' + path.lstrip('/')
    if params:
        url += '?' + urllib.parse.urlencode(params)
    return url
