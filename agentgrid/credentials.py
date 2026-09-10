"""OpenRouter credential storage. Secrets never travel through command arguments."""
from __future__ import annotations

import ctypes
import os
import sys

SERVICE = b"AgentGrid.OpenRouter"
ACCOUNT = b"api-key"
NOT_FOUND = -25300


def _security():
    if sys.platform != "darwin":
        raise ValueError("Secure storage is available on macOS. Set OPENROUTER_API_KEY in the server environment on other systems.")
    lib = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
    ptr = ctypes.c_void_p
    lib.SecKeychainFindGenericPassword.argtypes = [ptr, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ptr), ctypes.POINTER(ptr)]
    lib.SecKeychainAddGenericPassword.argtypes = [ptr, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ptr)]
    lib.SecKeychainItemModifyAttributesAndData.argtypes = [ptr, ptr, ctypes.c_uint32, ctypes.c_char_p]
    lib.SecKeychainItemFreeContent.argtypes = [ptr, ptr]
    lib.SecKeychainItemDelete.argtypes = [ptr]
    for name in ('SecKeychainFindGenericPassword', 'SecKeychainAddGenericPassword',
                 'SecKeychainItemModifyAttributesAndData', 'SecKeychainItemFreeContent', 'SecKeychainItemDelete'):
        getattr(lib, name).restype = ctypes.c_int32
    return lib


def _release(item):
    if item:
        lib = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        lib.CFRelease.argtypes = [ctypes.c_void_p]
        lib.CFRelease.restype = None
        lib.CFRelease(item)


def stored_key():
    if sys.platform != 'darwin':
        return ''
    lib = _security(); length = ctypes.c_uint32(); data = ctypes.c_void_p()
    status = lib.SecKeychainFindGenericPassword(None, len(SERVICE), SERVICE, len(ACCOUNT), ACCOUNT,
                                               ctypes.byref(length), ctypes.byref(data), None)
    if status == NOT_FOUND:
        return ''
    if status != 0:
        raise ValueError("Could not read OpenRouter key from Keychain. Unlock Keychain and try again.")
    try:
        return ctypes.string_at(data, length.value).decode('utf-8')
    finally:
        lib.SecKeychainItemFreeContent(None, data)


def get_key():
    return os.environ.get('OPENROUTER_API_KEY', '').strip() or stored_key()


def save_key(key):
    key = key.strip()
    if not key or len(key) > 4096 or any(c.isspace() for c in key):
        raise ValueError('Enter a valid OpenRouter API key.')
    lib = _security(); item = ctypes.c_void_p(); secret = key.encode()
    status = lib.SecKeychainFindGenericPassword(None, len(SERVICE), SERVICE, len(ACCOUNT), ACCOUNT,
                                               None, None, ctypes.byref(item))
    try:
        if status == NOT_FOUND:
            status = lib.SecKeychainAddGenericPassword(None, len(SERVICE), SERVICE, len(ACCOUNT), ACCOUNT,
                                                       len(secret), secret, None)
        elif status == 0:
            status = lib.SecKeychainItemModifyAttributesAndData(item, None, len(secret), secret)
        if status != 0:
            raise ValueError('Could not save the key in macOS Keychain.')
    finally:
        _release(item)


def delete_key():
    lib = _security(); item = ctypes.c_void_p()
    status = lib.SecKeychainFindGenericPassword(None, len(SERVICE), SERVICE, len(ACCOUNT), ACCOUNT,
                                               None, None, ctypes.byref(item))
    try:
        if status == NOT_FOUND:
            return
        if status != 0 or lib.SecKeychainItemDelete(item) != 0:
            raise ValueError('Could not remove the saved OpenRouter key.')
    finally:
        _release(item)


def connection_status():
    if os.environ.get('OPENROUTER_API_KEY', '').strip():
        return {'connected': True, 'source': 'environment', 'canSave': sys.platform == 'darwin'}
    return {'connected': bool(stored_key()), 'source': 'keychain', 'canSave': sys.platform == 'darwin'}
