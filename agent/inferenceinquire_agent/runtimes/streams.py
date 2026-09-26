"""A running log tail that the agent owns, and can end from any thread.

Every runtime hands the journal follower one of these. Iterating it yields the
log's lines; stop() ends the tail without touching the reader, so the watchdog
thread can call it while run() is blocked mid-read; close() is the reader's
own cleanup once it has stopped reading.
"""

import os
import signal
import subprocess


class ProcessStream:
    """A tail that is a command: journalctl, directly or inside a container.

    The command starts in a session of its own, so everything it starts is in
    one process group - including a journalctl that `lxc-attach` runs inside
    a container, which holds the pipe open by itself and outlives a wrapper
    that is only terminated. Ending the group ends exactly this tail. Matching
    a command line instead (`pkill -f`) also caught another agent's tail on
    the same host.
    """

    def __init__(self, argv):
        self.proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self.stopped = False

    def __iter__(self):
        return iter(self.proc.stdout)

    def _signal_group(self, sig):
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self):
        """End every writer, so the reader sees EOF. Idempotent.

        The group is signalled before the leader is reaped: until then its pid,
        and so the group id, cannot be reused by an unrelated process.
        """
        if self.stopped:
            return
        self.stopped = True
        self._signal_group(signal.SIGTERM)
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._signal_group(signal.SIGKILL)
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

    def close(self):
        self.stop()
        try:
            if self.proc.stdout:
                self.proc.stdout.close()
        except Exception:
            pass
