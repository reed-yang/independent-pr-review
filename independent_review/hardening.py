"""Process hardening and one-shot secret intake for inference phases."""

import ctypes
import os
import sys

from .core import ReviewError


PR_GET_DUMPABLE, PR_SET_DUMPABLE = 3, 4


def harden_process():
    """Stop same-uid processes from reading this process's /proc environ and memory.

    Call before any secret is read. Returns a short status string.
    """
    if not sys.platform.startswith('linux'):
        return 'unsupported_platform'
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0 or libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) != 0:
            raise ReviewError('process_hardening_failed')
    except (OSError, AttributeError):
        raise ReviewError('process_hardening_failed') from None
    return 'linux_nondumpable'


def take_secret(name):
    """Return an environment secret and remove it from this process's environment."""
    value = os.environ.pop(name, '') if name else ''
    if not value:
        raise ReviewError('backend_not_configured')
    return value
