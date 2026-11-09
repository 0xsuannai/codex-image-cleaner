"""One desktop instance per data profile; the OS releases ownership after a crash."""

import hashlib
import os
import threading
from pathlib import Path

_registry = set()
_registry_lock = threading.Lock()


class AlreadyRunning(Exception):
    pass


class SingleInstance:
    def __init__(self, profile: Path):
        self.profile = profile.resolve()
        self.handle = None
        self.stream = None
        self.kernel = None
        self.owned = False
        self.claimed = False
        self.key = str(self.profile).casefold() if os.name == "nt" else str(self.profile)

    def __enter__(self):
        self.profile.mkdir(parents=True, exist_ok=True)
        # Windows mutexes are recursive for one thread. The in-process registry
        # prevents a second desktop in that same thread from recursively owning it.
        with _registry_lock:
            if self.key in _registry:
                raise AlreadyRunning("此数据目录的清理工具已经打开")
            _registry.add(self.key)
            self.claimed = True
        try:
            self.acquire_os_lock()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def acquire_os_lock(self):
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
            self.kernel.CreateMutexW.restype = wintypes.HANDLE
            self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            self.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
            self.kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
            key = hashlib.sha256(str(self.profile).casefold().encode("utf-8")).hexdigest()[:32]
            ctypes.set_last_error(0)
            self.handle = self.kernel.CreateMutexW(None, False, "Local\\CodexImageCleaner-" + key)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            # Object existence is not ownership. A terminated owner can leave an
            # abandoned mutex; WAIT_ABANDONED grants us ownership safely.
            status = self.kernel.WaitForSingleObject(self.handle, 0)
            if status == 0x102:
                raise AlreadyRunning("此数据目录的清理工具已经打开")
            if status not in (0, 0x80):
                raise ctypes.WinError(ctypes.get_last_error())
            self.owned = True
        else:
            import fcntl

            self.stream = (self.profile / "desktop.lock").open("a+b")
            try:
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise AlreadyRunning("此数据目录的清理工具已经打开") from exc
            self.owned = True

    def __exit__(self, *_):
        if self.handle:
            if self.owned:
                self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        if self.stream:
            self.stream.close()
            self.stream = None
        self.owned = False
        if self.claimed:
            with _registry_lock:
                _registry.discard(self.key)
            self.claimed = False
