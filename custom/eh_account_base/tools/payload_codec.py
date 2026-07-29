# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Compression codec for cached report payloads.

zlib gives us roughly 5x to 10x ratio on JSON heavy report data with cheap
CPU. The codec is centralised here so the orchestrator and the audit model
agree on the format. Forward compatibility: a one byte version prefix
identifies the codec, so future formats (lz4, zstd) can coexist without
breaking stored cache entries.
"""

import json
import zlib

# Codec version byte. Increment when changing the encoding so old payloads
# can be detected and either decoded with the legacy path or invalidated.
_CODEC_VERSION_ZLIB_JSON = 0x01


def compress_payload(payload):
    """Serialise a Python value to a compressed bytes blob.

    :param payload: any JSON serialisable Python object.
    :return: bytes (a one byte version prefix followed by the compressed body).
    """
    if payload is None:
        return None
    body = json.dumps(payload, separators=(',', ':'), default=str).encode('utf-8')
    return bytes([_CODEC_VERSION_ZLIB_JSON]) + zlib.compress(body)


def decompress_payload(blob):
    """Decompress a payload produced by compress_payload().

    Returns None if blob is None or empty. Raises ValueError if the version
    byte is unknown.
    """
    if not blob:
        return None
    import base64
    # Odoo 19 Binary fields surface as base64 strings or base64 bytes on
    # read. If the first byte is not our version marker, base64-decode.
    if isinstance(blob, str):
        blob = base64.b64decode(blob)
    elif isinstance(blob, (bytes, bytearray)) and (
        not blob or blob[0] != _CODEC_VERSION_ZLIB_JSON
    ):
        try:
            blob = base64.b64decode(blob)
        except Exception:  # noqa: BLE001
            pass
    if not blob:
        return None
    version = blob[0]
    body = blob[1:]
    if version == _CODEC_VERSION_ZLIB_JSON:
        decoded = zlib.decompress(body).decode('utf-8')
        return json.loads(decoded)
    raise ValueError(f"unknown payload codec version: {version!r}")
