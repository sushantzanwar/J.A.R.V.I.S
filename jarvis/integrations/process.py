"""Run a child process so that it, and everything it starts, cannot outlive its purpose.

On Windows the child goes into a Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: on timeout the whole job
is terminated, and if this process dies (crash, kill, logoff) the OS closes the job handle and kills the child
tree. Children get no console window (CREATE_NO_WINDOW), which matters when started by the scheduler.
Elsewhere this falls back to subprocess.run.
"""
import subprocess
import sys
import os
from pathlib import Path
from types import SimpleNamespace

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _Io(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                                                   "OtherOperationCount", "ReadTransferCount",
                                                   "WriteTransferCount", "OtherTransferCount")]

    class _Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]

    _KILL_ON_JOB_CLOSE = 0x2000
    _EXTENDED_LIMIT_INFORMATION = 9

    def _new_job():
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(job, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)):
            _k32.CloseHandle(job)
            return None
        return job


def run_contained(argv, *, input=None, timeout=None, cwd=None, env=None, stdin=None, **_ignored):
    """subprocess.run(capture_output=True, text=True) with containment. Raises subprocess.TimeoutExpired
    after killing the whole child tree. The result has .returncode, .stdout, .stderr and .contained."""
    child_env = dict(os.environ if env is None else env)
    # The parent explicitly uses UTF-8 pipes. Make a Python child use the same
    # codec on Windows, where its inherited ANSI code page can otherwise turn
    # valid UTF-8 task input into replacement characters.
    if argv and Path(str(argv[0])).name.lower().startswith("python"):
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
    if sys.platform != "win32":
        return subprocess.run(argv, input=input, timeout=timeout, cwd=cwd, env=child_env, stdin=stdin,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
    job = _new_job()
    p = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else (stdin or subprocess.DEVNULL),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=child_env, text=True,
                         encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
    contained = bool(job) and bool(_k32.AssignProcessToJobObject(job, int(p._handle)))
    try:
        try:
            out, err = p.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            if contained:
                _k32.TerminateJobObject(job, 1)
            p.kill()
            try:
                out, err = p.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err) from None
        return SimpleNamespace(args=argv, returncode=p.returncode, stdout=out, stderr=err, contained=contained)
    finally:
        if job:
            _k32.CloseHandle(job)  # KILL_ON_JOB_CLOSE: anything the child left running dies here
