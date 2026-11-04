"""Always-on ingest worker: live feeds on their intervals, block profiles every 6 h, one instance via an ES lease.

    python worker.py          run forever; SIGTERM/SIGINT releases the lease and exits
    python worker.py --once   run every feed once and exit (CI / demo)
"""

import argparse
import logging
import os
import signal
import socket
import sys
import threading
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.base import STATE_RUNNING
from apscheduler.schedulers.blocking import BlockingScheduler
from elasticsearch import ConflictError, NotFoundError

from highground import feeds, replay, store
from highground.config import settings

log = logging.getLogger("highground.worker")

LOCK = "worker"  # lease doc id in the locks index
LEASE_TTL_S = 120  # lease lifetime: a dead holder stops renewing, so its lease expires
RENEW_EVERY_S = 30  # the holder renews the lease every 30 s
ACQUIRE_POLL_S = 5  # a second worker polls until the lease expires
PROFILES_EVERY_S = 6 * 3600


class Lease:
    """Single-instance lease: doc `worker` in settings().ix("locks"), created with op_type=create and expires_at 120 s ahead."""

    def __init__(self, s: store.Store):
        self.s = s
        self.index = settings().ix("locks")
        self.seq_no: int | None = None
        self.primary_term: int | None = None

    def ensure_index(self) -> None:
        if not self.s.es.indices.exists(index=self.index):
            self.s.es.indices.create(index=self.index, mappings={"properties": {"expires_at": {"type": "date"}, "pid": {"type": "integer"}, "host": {"type": "keyword"}}})

    def _doc(self) -> dict:
        return {"expires_at": (datetime.now(UTC) + timedelta(seconds=LEASE_TTL_S)).isoformat(), "pid": os.getpid(), "host": socket.gethostname()}

    def _create(self) -> bool:
        try:
            r = self.s.es.index(index=self.index, id=LOCK, op_type="create", document=self._doc(), refresh=True)
        except ConflictError:
            return False
        self.seq_no, self.primary_term = r["_seq_no"], r["_primary_term"]
        return True

    def _take_over_if_expired(self) -> bool:
        now = datetime.now(UTC)
        try:
            r = self.s.es.get(index=self.index, id=LOCK)
        except NotFoundError:
            return False
        expires = r["_source"].get("expires_at")
        if not expires or datetime.fromisoformat(expires) > now:
            return False  # holder still alive
        try:
            self.s.es.delete(index=self.index, id=LOCK, if_seq_no=r["_seq_no"], if_primary_term=r["_primary_term"], refresh=True)
        except ConflictError:
            return False  # a third worker won the delete race; the op_type=create in _create still arbitrates
        return self._create()

    def acquire(self, stop: threading.Event) -> bool:
        """Block until we hold the lease; a second worker waits here until the old lease expires."""
        waited = False
        while not stop.is_set():
            if self._create() or self._take_over_if_expired():
                log.info("lease acquired %s/%s ttl=%ds", self.index, LOCK, LEASE_TTL_S)
                return True
            if not waited:
                log.info("lease %s/%s held by another worker, waiting for it to expire", self.index, LOCK)
                waited = True
            stop.wait(ACQUIRE_POLL_S)
        return False

    def renew(self) -> bool:
        try:
            r = self.s.es.index(index=self.index, id=LOCK, document=self._doc(), if_seq_no=self.seq_no, if_primary_term=self.primary_term, refresh=True)
        except (ConflictError, NotFoundError):
            return False  # doc gone or owned by a successor: we are no longer the holder
        self.seq_no, self.primary_term = r["_seq_no"], r["_primary_term"]
        return True

    def release(self) -> None:
        if self.seq_no is None:
            return
        try:
            self.s.es.delete(index=self.index, id=LOCK, if_seq_no=self.seq_no, if_primary_term=self.primary_term, refresh=True)
            log.info("lease released %s/%s", self.index, LOCK)
        except (ConflictError, NotFoundError):
            pass  # already expired or owned by a successor
        self.seq_no = None


def feed_job(name: str, http) -> None:
    """One scheduled feed run. run_feed records feed errors in the feeds index; this must never raise into the scheduler."""
    try:
        log.info("feed %s %s", name, store.run_feed(store.Store(), name, http))
    except Exception as exc:  # noqa: BLE001
        log.error("feed %s crashed %s: %s", name, type(exc).__name__, exc)


def profiles_job(s: store.Store) -> None:
    try:
        out = replay.build_profiles(s, datetime.now(UTC), index="profiles")
        log.info("profiles cells=%d storm_days=%d", out["cells"], out["storm_days"])
    except Exception as exc:  # noqa: BLE001
        log.error("profiles crashed %s: %s", type(exc).__name__, exc)


def renew_job(sched: BlockingScheduler, lease: Lease, stop: threading.Event) -> None:
    try:
        ok = lease.renew()
    except Exception as exc:  # noqa: BLE001 - transient ES error: retry next cycle, the 120 s TTL absorbs one miss
        log.warning("lease renew error %s: %s", type(exc).__name__, exc)
        return
    if not ok:
        log.error("lease lost to another worker, shutting down")
        stop.set()
        if sched.state == STATE_RUNNING:
            sched.shutdown(wait=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run every feed once and exit (CI/demo); no lease, feeds are idempotent")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    for noisy in ("apscheduler", "elastic_transport", "httpx"):  # keep the log at one line per job result
        logging.getLogger(noisy).setLevel(logging.WARNING)
    s = store.Store()
    http = feeds.client()  # one shared HTTP client for every feed

    if args.once:
        for name in feeds.FEEDS:
            feed_job(name, http)
        http.close()
        return 0

    stop = threading.Event()
    sched = BlockingScheduler()

    def handle_signal(signum, _frame) -> None:
        log.info("signal %s, shutting down", signal.Signals(signum).name)
        stop.set()
        if sched.state == STATE_RUNNING:
            sched.shutdown(wait=False)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    lease = Lease(s)
    lease.ensure_index()
    if not lease.acquire(stop):
        http.close()
        return 0

    try:
        now = datetime.now(UTC)
        for name, (interval, _, _) in feeds.FEEDS.items():
            sched.add_job(feed_job, "interval", args=[name, http], id=f"feed:{name}", seconds=interval, next_run_time=now, max_instances=1, coalesce=True)
        sched.add_job(profiles_job, "interval", args=[s], id="profiles", seconds=PROFILES_EVERY_S, next_run_time=now, max_instances=1, coalesce=True)
        sched.add_job(renew_job, "interval", args=[sched, lease, stop], id="lease", seconds=RENEW_EVERY_S,
                      next_run_time=now + timedelta(seconds=RENEW_EVERY_S), max_instances=1, coalesce=True)
        log.info("worker up: feeds %s, profiles every %ds, lease %s/%s", {n: i for n, (i, _, _) in feeds.FEEDS.items()}, PROFILES_EVERY_S, lease.index, LOCK)
        sched.start()  # blocks until SIGTERM/SIGINT or lease loss
    finally:
        lease.release()
        http.close()
    log.info("worker down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
