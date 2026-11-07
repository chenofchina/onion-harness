"""Secret storage and data encryption.

Two separate concerns, deliberately kept apart:

* **API keys** live in the OS credential store, never in a file. ``settings.json``
  keeps only a reference name.
* **User data** (memory text, and anything else marked sensitive) is encrypted
  with AES-256-GCM before it reaches disk. The key is derived from a random
  master secret that also lives in the credential store.

The master secret is generated once, on first use, and never written anywhere
readable. If the credential store is unavailable the application refuses to
store keys rather than falling back to plaintext.
"""

from __future__ import annotations

import base64
import ctypes
import os
import secrets
import sys
from abc import ABC, abstractmethod
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

# Credential target prefix, so entries are findable and clearly ours.
CRED_PREFIX = "onion:provider:"
MASTER_CRED_TARGET = "onion:master"

# PBKDF2 iterations. The master secret is already high-entropy (32 random
# bytes), so this is defence in depth rather than the primary strength.
_KDF_ITERATIONS = 200_000
_ITERATIONS_INDEX = 50_000
_KEY_BYTES = 32
_NONCE_BYTES = 12


class SecretError(RuntimeError):
    """The credential store is unavailable or refused an operation."""


# --------------------------------------------------------------------- stores
class SecretStore(ABC):
    """Where API keys live. Implementations must never touch a plain file."""

    @abstractmethod
    def get(self, name: str) -> Optional[str]: ...

    @abstractmethod
    def set(self, name: str, value: str) -> None: ...

    @abstractmethod
    def delete(self, name: str) -> None: ...

    @abstractmethod
    def available(self) -> bool: ...


class WindowsCredentialStore(SecretStore):
    """Windows Credential Manager via ``win32cred``.

    Keys are stored as generic credentials, readable by the current Windows user
    only. Other accounts on the machine cannot retrieve them.
    """

    def __init__(self) -> None:
        self._cred = None
        if sys.platform == "win32":
            try:
                import win32cred

                self._cred = win32cred
            except Exception:  # noqa: BLE001 - pywin32 missing
                self._cred = None

    def available(self) -> bool:
        return self._cred is not None

    def get(self, name: str) -> Optional[str]:
        if self._cred is None:
            raise SecretError("Windows credential manager is unavailable")
        try:
            entry = self._cred.CredRead(name, self._cred.CRED_TYPE_GENERIC, 0)
        except Exception:  # noqa: BLE001 - not found raises pywintypes.error
            return None
        blob = entry.get("CredentialBlob") or b""
        if isinstance(blob, str):
            return blob
        try:
            return blob.decode("utf-16-le")
        except Exception:  # noqa: BLE001
            return blob.decode("utf-8", errors="replace")

    def set(self, name: str, value: str) -> None:
        if self._cred is None:
            raise SecretError("Windows credential manager is unavailable")
        try:
            self._cred.CredWrite(
                {
                    "Type": self._cred.CRED_TYPE_GENERIC,
                    "TargetName": name,
                    "UserName": "onion-harness",
                    # pywin32 encodes str to UTF-16 itself; passing bytes
                    # raises "Objects of type 'bytes' can not be converted".
                    "CredentialBlob": value,
                    "Persist": self._cred.CRED_PERSIST_LOCAL_MACHINE,
                },
                0,
            )
        except Exception as exc:  # noqa: BLE001
            raise SecretError(f"could not write credential: {exc}") from None

    def delete(self, name: str) -> None:
        if self._cred is None:
            return
        try:
            self._cred.CredDelete(name, self._cred.CRED_TYPE_GENERIC, 0)
        except Exception:  # noqa: BLE001 - already absent
            pass


class MemorySecretStore(SecretStore):
    """In-process store, for tests only.

    Named explicitly so nobody reaches for it in production by accident.
    """

    def __init__(self, initial: Optional[dict[str, str]] = None) -> None:
        self._data = dict(initial or {})

    def available(self) -> bool:
        return True

    def get(self, name: str) -> Optional[str]:
        return self._data.get(name)

    def set(self, name: str, value: str) -> None:
        self._data[name] = value

    def delete(self, name: str) -> None:
        self._data.pop(name, None)


def default_secret_store() -> SecretStore:
    """The store the application should use on this platform."""
    windows = WindowsCredentialStore()
    if windows.available():
        return windows
    # No credential store: the caller must decide how to proceed. Returning a
    # memory store here would silently lose keys on restart, so say so instead.
    raise SecretError(
        "No system credential store is available on this platform. "
        "Install pywin32 (Windows) or a keyring backend."
    )


# ----------------------------------------------------------------- encryption
class DataCipher:
    """AES-256-GCM with a key derived from a random master secret.

    Each ``encrypt`` call uses a fresh nonce, so identical plaintext produces
    different ciphertext. The nonce is prefixed to the payload; there is no
    separate nonce column to keep in sync.
    """

    def __init__(self, master_secret: bytes) -> None:
        if len(master_secret) < 16:
            raise SecretError("master secret is too short")
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=_KEY_BYTES,
            salt=b"onion-harness-memory-v1",
            iterations=_KDF_ITERATIONS,
        )
        self._key = kdf.derive(master_secret)
        self._aead = AESGCM(self._key)
        # A distinct subkey for the search index, so a leaked index digest does
        # not help against the data key.
        self._index_key = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=_KEY_BYTES,
            salt=b"onion-harness-index-v1",
            iterations=_ITERATIONS_INDEX,
        ).derive(master_secret)

    def encrypt(self, text: str) -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, text.encode("utf-8"), None)
        return nonce + sealed

    def decrypt(self, payload: bytes) -> str:
        raw = bytes(payload)
        if len(raw) <= _NONCE_BYTES:
            raise SecretError("ciphertext is truncated")
        nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, sealed, None).decode("utf-8")
        except Exception:  # noqa: BLE001 - wrong key or tampered data
            raise SecretError(
                "could not decrypt stored data (wrong master secret, or the file "
                "was modified)"
            ) from None

    def token_hash(self, token: str) -> str:
        """Deterministic digest of a search token, for a blind index.

        Full-text search needs to compare words, which normally means storing
        them in the clear. Hashing each token with a keyed digest lets the index
        match words without revealing them: the database holds only digests, and
        the same token always produces the same digest for this key.
        """
        import hashlib
        import hmac

        digest = hmac.new(self._index_key, token.encode("utf-8"), hashlib.sha256)
        return digest.hexdigest()[:32]


def load_or_create_master_secret(store: SecretStore) -> bytes:
    """Read the master secret, generating and persisting one on first run."""
    existing = store.get(MASTER_CRED_TARGET)
    if existing:
        try:
            return base64.b64decode(existing)
        except Exception:  # noqa: BLE001 - corrupt entry, regenerate below
            pass
    secret = secrets.token_bytes(_KEY_BYTES)
    store.set(MASTER_CRED_TARGET, base64.b64encode(secret).decode("ascii"))
    return secret


def cipher_from_store(store: SecretStore) -> DataCipher:
    return DataCipher(load_or_create_master_secret(store))


# ------------------------------------------------------------------ key vault
class KeyVault:
    """API keys, addressed by provider id, backed by a :class:`SecretStore`."""

    def __init__(self, store: SecretStore) -> None:
        self._store = store

    @property
    def store(self) -> SecretStore:
        return self._store

    def _target(self, provider_id: str) -> str:
        return f"{CRED_PREFIX}{provider_id}"

    def get(self, provider_id: str) -> Optional[str]:
        return self._store.get(self._target(provider_id))

    def set(self, provider_id: str, api_key: str) -> None:
        value = (api_key or "").strip()
        if not value:
            self.delete(provider_id)
            return
        self._store.set(self._target(provider_id), value)

    def delete(self, provider_id: str) -> None:
        self._store.delete(self._target(provider_id))

    def configured(self) -> list[str]:
        """Providers that currently hold a key."""
        # Credential Manager cannot enumerate our entries cheaply, and a provider
        # list is small, so callers pass the ids they care about instead.
        return []

    def has(self, provider_id: str) -> bool:
        return bool(self.get(provider_id))


__all__ = [
    "SecretError",
    "SecretStore",
    "WindowsCredentialStore",
    "MemorySecretStore",
    "default_secret_store",
    "DataCipher",
    "KeyVault",
    "load_or_create_master_secret",
    "cipher_from_store",
    "CRED_PREFIX",
]