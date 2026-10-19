"""
Secrets at rest (connection passwords, S3 keys, LLM API keys, model API keys) are Fernet-encrypted before
they reach PostgreSQL, so a database dump alone does not leak them.

The key comes from DATAVIZ_SECRET_KEY (one key, or several comma-separated: the first encrypts, all of them
decrypt, which is how a key is rotated). Without it a key is generated once into <workspace_root>/secret.key
(mode 0600) — fine for one server, but back that file up together with the database.
"""

import os
from pathlib import Path

import orjson
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_fernet = None


def init(env_key=None, key_file=None, log=None):
    """Set up the key ring; returns 'env' or 'file:<path>' (where the key came from)."""
    global _fernet
    raw = (env_key if env_key is not None else os.environ.get('DATAVIZ_SECRET_KEY', '')).strip()
    where = 'env'
    if not raw:
        if key_file is None:
            raise RuntimeError('DATAVIZ_SECRET_KEY is not set and no key file was given')
        p = Path(key_file)
        if p.exists():
            raw = p.read_text().strip()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            raw = Fernet.generate_key().decode()
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)      # never world-readable
            with os.fdopen(fd, 'w') as f:
                f.write(raw + '\n')
            if log:
                log(f'generated a new secret key in {p} (set DATAVIZ_SECRET_KEY to manage it yourself; back it up)')
        where = f'file:{p}'
    try:
        _fernet = MultiFernet([Fernet(k.strip().encode()) for k in raw.split(',') if k.strip()])
    except (ValueError, TypeError) as e:
        raise RuntimeError('DATAVIZ_SECRET_KEY is not a valid Fernet key (32 url-safe base64 bytes; generate one with '
                           'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")') from e
    return where


def _f():
    if _fernet is None:
        raise RuntimeError('crypto.init() was not called')
    return _fernet


def encrypt_json(obj):
    """dict -> ciphertext bytes; None / empty dict -> None (nothing secret to store)."""
    if not obj:
        return None
    return _f().encrypt(orjson.dumps(obj))


def decrypt_json(blob):
    """ciphertext bytes -> dict; None -> {}. A blob from another key is an error, not silently empty."""
    if not blob:
        return {}
    try:
        return orjson.loads(_f().decrypt(bytes(blob)))
    except InvalidToken as e:
        raise RuntimeError('a stored secret cannot be decrypted: DATAVIZ_SECRET_KEY changed? '
                           '(add the old key after the new one, comma-separated)') from e
