# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Shared AES-256-GCM credential vault for the ERP Heritage e-invoicing
modules.

One implementation, parametrised per module by a key namespace, so the
Mauritius, France, and Peppol modules encrypt secrets with a single
audited copy of the crypto instead of three forks.

Stored ciphertext format: 12-byte nonce || ciphertext. The AES-GCM
ciphertext already carries its 16-byte authentication tag appended at the
end, so the layout is nonce-then-ciphertext-with-tag, base64-encoded for
DB storage. (This is the exact byte layout the standalone module vaults
produced, so ciphertext written before this refactor decrypts unchanged.)

Each module binds its own master key resolution via ``for_namespace``:

    vault = credential_vault.for_namespace('mu_einv', 'eh_l10n_mu_einvoicing')

which resolves the 32-byte master key, in order, from:

1. Environment variable EH_<NAMESPACE_UPPER>_MASTER_KEY (base64 32 bytes),
   for example EH_MU_EINV_MASTER_KEY.
2. File /etc/odoo/eh_<namespace>.master.key (0600 perms required), for
   example /etc/odoo/eh_mu_einv.master.key.
3. ir.config_parameter <module>.master_key_b64 (fallback; not recommended
   for production because it stores the key in the same database as the
   ciphertext), for example eh_l10n_mu_einvoicing.master_key_b64.

The namespace and module strings MUST stay stable for an existing module.
Changing the derived env var name, file path, or parameter key would make
the master key fail to resolve and already-stored ciphertext
unrecoverable.

Production hardening: put the master key on disk OR in the OS keyring and
grant the Odoo process user read access only. Never commit it to Git or
let it land in pg_dump output.

Generate a master key with:
    python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
"""

import base64
import binascii
import logging
import os
import stat

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_logger = logging.getLogger(__name__)

_NONCE_LEN = 12
_TAG_LEN = 16
_KEY_LEN = 32  # AES-256


class VaultError(Exception):
    pass


def _decode_key(raw, source):
    try:
        key = base64.b64decode(raw)
    except (ValueError, binascii.Error) as exc:
        raise VaultError(
            "Master key (%s) is not valid base64: %s" % (source, exc)
        )
    if len(key) != _KEY_LEN:
        raise VaultError(
            "Master key (%s) must decode to %d bytes; got %d."
            % (source, _KEY_LEN, len(key))
        )
    return key


class Vault:
    """An AES-256-GCM vault bound to one module's master key sources.

    Construct via :func:`for_namespace` (preferred) or directly with the
    three resolution identifiers. The encrypt / decrypt / is_configured
    API is identical to the original per-module vault modules so existing
    call sites do not change.
    """

    def __init__(self, env_var, key_file, param_key):
        self._env_var = env_var
        self._key_file = key_file
        self._param_key = param_key

    def _resolve_master_key(self, env=None):
        """Return the 32-byte master key as bytes, or raise VaultError.

        ``env`` is the Odoo env; only consulted for the
        ir.config_parameter fallback so the vault can be unit-tested
        without the ORM.
        """
        raw = os.environ.get(self._env_var)
        if raw:
            return _decode_key(raw, source='env')

        if os.path.exists(self._key_file):
            try:
                st = os.stat(self._key_file)
            except OSError as exc:
                raise VaultError(
                    "Cannot stat master key file %s: %s"
                    % (self._key_file, exc)
                )
            # File must be 0600 or stricter; refuse otherwise.
            if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise VaultError(
                    "Master key file %s has loose permissions; refuse to "
                    "read. Set chmod 0600 first." % self._key_file
                )
            with open(self._key_file, 'r') as fh:
                return _decode_key(fh.read().strip(), source='file')

        if env is not None:
            raw = env['ir.config_parameter'].sudo().get_param(self._param_key)
            if raw:
                _logger.warning(
                    "%s master key read from ir.config_parameter; this is "
                    "the LEAST SECURE option. Move it to %s or %s.",
                    self._param_key, self._env_var, self._key_file,
                )
                return _decode_key(raw, source='config_parameter')

        raise VaultError(
            "No master key configured. Set %s, write %s (chmod 0600), or "
            "set the ir.config_parameter %s. The vault refuses to operate "
            "without a master key."
            % (self._env_var, self._key_file, self._param_key)
        )

    def is_configured(self, env=None):
        """Return True if a master key can be resolved, without raising."""
        try:
            self._resolve_master_key(env)
            return True
        except VaultError:
            return False

    def encrypt(self, plaintext, env=None, associated_data=b''):
        """Encrypt a string. Returns base64-encoded ciphertext (or '').

        ``associated_data`` is bound into the GCM tag; pass a per-record
        value such as f"company:{company.id}" so a ciphertext copied to a
        different record cannot be reused.
        """
        if plaintext is None or plaintext == '':
            return ''
        if isinstance(plaintext, str):
            plaintext = plaintext.encode('utf-8')
        if isinstance(associated_data, str):
            associated_data = associated_data.encode('utf-8')
        key = self._resolve_master_key(env)
        aes = AESGCM(key)
        nonce = os.urandom(_NONCE_LEN)
        ciphertext = aes.encrypt(nonce, plaintext, associated_data)
        blob = nonce + ciphertext  # ciphertext already carries the 16-byte tag
        return base64.b64encode(blob).decode('ascii')

    def decrypt(self, ciphertext_b64, env=None, associated_data=b''):
        """Reverse of :meth:`encrypt`. Returns the plaintext string (or '').

        Raises VaultError if the tag check fails (ciphertext tampered with
        or the associated_data does not match).
        """
        if not ciphertext_b64:
            return ''
        if isinstance(associated_data, str):
            associated_data = associated_data.encode('utf-8')
        try:
            blob = base64.b64decode(ciphertext_b64)
        except (ValueError, binascii.Error) as exc:
            raise VaultError("Vault ciphertext is not valid base64: %s" % exc)
        if len(blob) < _NONCE_LEN + _TAG_LEN:
            raise VaultError("Vault ciphertext is too short")
        nonce = blob[:_NONCE_LEN]
        body = blob[_NONCE_LEN:]
        key = self._resolve_master_key(env)
        aes = AESGCM(key)
        try:
            plaintext = aes.decrypt(nonce, body, associated_data)
        except Exception as exc:
            raise VaultError(
                "Vault decryption failed (tag check). The ciphertext is "
                "corrupted or the associated_data does not match: %s" % exc
            )
        return plaintext.decode('utf-8')


def for_namespace(namespace, module):
    """Return a :class:`Vault` bound to a module's master key sources.

    ``namespace`` derives the environment variable and the key file path;
    ``module`` derives the ir.config_parameter key. The mapping is fixed
    and must not change for an existing module, or its stored ciphertext
    becomes unrecoverable:

    * env var:   EH_<NAMESPACE_UPPER>_MASTER_KEY
    * key file:  /etc/odoo/eh_<namespace>.master.key
    * param key: <module>.master_key_b64
    """
    return Vault(
        env_var='EH_%s_MASTER_KEY' % namespace.upper(),
        key_file='/etc/odoo/eh_%s.master.key' % namespace,
        param_key='%s.master_key_b64' % module,
    )
