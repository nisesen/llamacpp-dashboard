"""Following the serving unit's log, wherever llama.cpp runs.

One tail at a time, on whichever llama* unit is active. A model switch changes
the active unit; the tail follows it. The runtime starts the tail (a
journalctl, or Docker's log stream) and hands back a stream it can end;
lines go to a JournalParser (parser.py), which keeps the state.
"""

import threading
import time

from ..cache import CACHES
from ..runtimes import follow
from .parser import JournalParser


class JournalFollower:
    """Follows the active llama.cpp unit's journal into a JournalParser."""

    # How often the watchdog re-checks which unit is active.
    RETARGET_POLL = 2.0

    def __init__(self):
        self.parser = JournalParser()
        # Guards self.stream/self.target: the watchdog thread ends the tail
        # while run() is reading it. Reentrant because _spawn calls _kill.
        self.proc_lock = threading.RLock()
        self.stream = None
        self.target = (None, None)  # (ctid, unit)
        # Whether our reader is sitting on an open tail. Not a process check:
        # under `pct exec` the wrapper can exit while journalctl keeps the
        # pipe open and keeps streaming.
        self.attached = False

    def _spawn(self, ctid, unit):
        with self.proc_lock:
            self._kill()
            # A load record belongs to the unit that emitted it. Following a
            # DIFFERENT unit makes the old record meaningless - and a unit
            # killed mid-load (exactly what a model switch does) never emits
            # "model loaded", so the record would otherwise sit at "loading"
            # forever and the banner would count up against a load that is not
            # happening. A same-unit respawn keeps the record: it is still true.
            if (ctid, unit) != self.target:
                self.parser.reset_load()
            try:
                self.stream = follow(ctid, unit)
                self.target = (ctid, unit)
            except Exception:
                self.stream = None

    def _kill(self, close_pipe=True):
        """End the tail: stop everything that writes into our stream.

        A STOPPED unit never writes again, so its tail would never see EPIPE
        on its own - and a stopped unit is exactly the case re-targeting
        exists for. The stream ends only the tail this agent started (its own
        process group, or its own socket), never another agent's.

        close_pipe=False is for the watchdog thread, which must never close
        the pipe while run() is blocked reading it: the buffered reader
        holds its lock across the blocking read, so close() waits for a line
        a stopped unit will never write. The watchdog deadlocked exactly
        there, holding proc_lock, on a live 27B -> Flash-Next switch, and
        request telemetry for the new model never started. Ending the writers
        gives the reader EOF instead, and run() closes the stream itself on
        its way out.
        """
        with self.proc_lock:
            stream = self.stream
            if not close_pipe:
                if stream is not None:
                    stream.stop()
                return
            if stream is not None:
                stream.close()
            self.stream = None
            self.attached = False

    def watch_target(self):
        """Ends the journal tail when the active unit changes.

        run() blocks reading stdout, and a STOPPED unit's journal never emits
        another line - so a switch away from the followed unit would park the
        reader on a dead unit indefinitely and silently end request telemetry
        for the unit that is actually serving. The in-loop check in run() only
        fires when a line arrives, which is precisely what stops happening.
        Ending the tail gives the reader EOF; run() then re-targets normally.
        """
        while True:
            time.sleep(self.RETARGET_POLL)
            try:
                guests, _ = CACHES["guests"].get()
                now = ((guests or {}).get("llm_ct") or {}).get("active_unit")
                with self.proc_lock:
                    if now and self.target[1] and now != self.target[1]:
                        self._kill(close_pipe=False)
            except Exception:
                pass

    def run(self):
        while True:
            guests, _ = CACHES["guests"].get()
            ct = (guests or {}).get("llm_ct") or {}
            ctid, unit = ct.get("vmid"), ct.get("active_unit")
            if not (ctid and unit):
                time.sleep(3)
                continue
            # Not a liveness check on a process: a `pct exec` wrapper exits
            # at once while the tail behind it lives on. _kill() below is what
            # ends a tail, and it clears self.stream - so "self.stream is None"
            # is the honest "we need a new tail" test.
            if (ctid, unit) != self.target or self.stream is None:
                self._spawn(ctid, unit)
                if self.stream is None:
                    time.sleep(5)
                    continue
            try:
                self.attached = True
                for line in self.stream:
                    self.parser.ingest(line.rstrip("\n"))
                    guests, _ = CACHES["guests"].get()
                    now_unit = ((guests or {}).get("llm_ct") or {}).get("active_unit")
                    if now_unit and now_unit != self.target[1]:
                        break  # model switched - re-target
            except Exception:
                pass
            finally:
                # EOF, re-target or error: this pipe is spent either way. Drop
                # the tail so the next pass spawns a fresh one, instead of
                # spinning on an exhausted descriptor.
                self._kill()
            time.sleep(1)

    def snapshot(self):
        return {
            "following": self.target[1],
            "ctid": self.target[0],
            "alive": bool(self.attached),
            **self.parser.snapshot(),
        }


JOURNAL = JournalFollower()
