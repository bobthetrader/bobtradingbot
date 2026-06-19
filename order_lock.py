import os
import time
import logging
from contextlib import contextmanager

try:
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None

_logger = logging.getLogger(__name__)
_warned_no_lock = False

# Use a platform-appropriate temp directory. /tmp doesn't exist on Windows;
# fall back to the OS temp dir there. This only matters for the fp.write()
# bookkeeping below -- on Windows the lock itself is a no-op regardless.
if os.name == 'nt':
    import tempfile
    LOCK_PATH = os.path.join(tempfile.gettempdir(), "kraken_order_executor.lock")
else:
    LOCK_PATH = "/tmp/kraken_order_executor.lock"
# If a lockfile is present but older than this TTL (seconds), consider it stale
# and attempt safe cleanup. This helps when a process crashes and leaves the
# lock file in place. Only used on platforms with `fcntl` available.
LOCK_TTL_SECONDS = 120


@contextmanager
def acquire_order_lock(timeout_seconds=5.0, poll_seconds=0.1):
    """Process-level lock to avoid concurrent AddOrder races across scripts/bot.

    NOTE: real locking only happens on platforms with `fcntl` (Linux/macOS).
    On Windows this is a best-effort no-op -- it always reports `locked=True`
    without preventing concurrent order placement across processes. This is
    fine for solo paper-mode testing on a single Windows box, but if you ever
    run multiple live-trading processes against the same account on Windows,
    this lock will NOT protect you from duplicate/concurrent orders.
    """
    global _warned_no_lock
    fp = None
    locked = False
    try:
        if fcntl is None:
            if not _warned_no_lock:
                _logger.warning(
                    "order_lock: fcntl unavailable on this platform (%s) -- "
                    "order locking is a NO-OP. Concurrent processes are NOT "
                    "protected from duplicate order placement.", os.name
                )
                _warned_no_lock = True
            # No flock support available -> best effort no-op
            yield True
            return

        os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
        fp = open(LOCK_PATH, "w")
        deadline = time.time() + max(0.0, float(timeout_seconds))

        while True:
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                fp.write(str(os.getpid()))
                fp.flush()
                break
            except BlockingIOError:
                # possible stale lock handling: inspect lockfile age and PID
                try:
                    if os.path.exists(LOCK_PATH):
                        age = time.time() - os.path.getmtime(LOCK_PATH)
                        if age > LOCK_TTL_SECONDS:
                            # read PID if present
                            try:
                                with open(LOCK_PATH, 'r') as rf:
                                    content = rf.read().strip()
                                    pid = int(content) if content else None
                            except Exception:
                                pid = None
                            stale_ok = False
                            if pid:
                                try:
                                    # signal 0 checks existence of process on Unix
                                    os.kill(pid, 0)
                                    # process exists -> not stale
                                    stale_ok = False
                                except Exception:
                                    # process not alive
                                    stale_ok = True
                            else:
                                # no pid recorded -> treat as stale by age
                                stale_ok = True

                            if stale_ok:
                                try:
                                    # attempt safe removal of lock file; ignore errors
                                    os.remove(LOCK_PATH)
                                    # reopen a fresh file descriptor for locking
                                    if fp:
                                        try:
                                            fp.close()
                                        except Exception:
                                            pass
                                    fp = open(LOCK_PATH, 'w')
                                    # loop back and try to acquire again immediately
                                    continue
                                except Exception:
                                    pass
                except Exception:
                    # fall through to normal wait behavior on any error
                    pass

                if time.time() >= deadline:
                    break
                time.sleep(max(0.01, float(poll_seconds)))

        yield locked
    finally:
        try:
            if fp and locked and fcntl is not None:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            if fp:
                fp.close()
        except Exception:
            pass
