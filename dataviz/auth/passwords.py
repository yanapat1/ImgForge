"""
Password hashing (argon2id, argon2-cffi defaults) and the password policy.

Hashes made with older parameters are re-hashed on the next successful login (needs_rehash). When the login
names no account, dummy_verify() still runs one verification so the answer takes as long either way.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LEN = 10
MAX_LEN = 256

_ph = PasswordHasher()           # tests swap this for one with other parameters (rehash path)
_dummy = {}                      # parameters -> a hash of a throw-away password


def hash_password(password):
    return _ph.hash(password)


def verify(stored, password):
    """True when `password` matches `stored`; any malformed / foreign hash is simply a mismatch."""
    if not stored or not isinstance(password, str):
        return False
    try:
        return _ph.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored):
    try:
        return _ph.check_needs_rehash(stored)
    except (InvalidHashError, ValueError):
        return True


def dummy_verify(password):
    """Burn one verification: an unknown login costs as much time as a wrong password."""
    key = (_ph.time_cost, _ph.memory_cost, _ph.parallelism)
    h = _dummy.get(key)
    if h is None:
        h = _dummy[key] = _ph.hash('dummy-password-for-timing')
    verify(h, password if isinstance(password, str) else '')
    return False


def check_policy(password, username=None, email=None):
    """ValueError with a human message when `password` is not acceptable."""
    if not isinstance(password, str) or len(password) < MIN_LEN:
        raise ValueError(f'the password must have at least {MIN_LEN} characters')
    if len(password) > MAX_LEN:
        raise ValueError(f'the password can have at most {MAX_LEN} characters')
    low = password.casefold()
    for other in (username, email):
        if other and low == str(other).casefold():
            raise ValueError('the password must not be your username or email')
