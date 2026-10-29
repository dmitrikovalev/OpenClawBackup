"""Which messages are already processed: SQLite, keyed by IMAP UIDVALIDITY + UID.

status "acked"   - the summary was delivered (POST /ack)
status "skipped" - the sender did not pass the filter
"""
import contextlib
import datetime as dt
import sqlite3

SCHEMA = """CREATE TABLE IF NOT EXISTS processed (
    uidvalidity INTEGER NOT NULL,
    uid         INTEGER NOT NULL,
    status      TEXT    NOT NULL,
    at          TEXT    NOT NULL,
    PRIMARY KEY (uidvalidity, uid))"""


class Store:
    def __init__(self, path):
        self.path = path
        with self._db() as db:
            db.execute(SCHEMA)

    @contextlib.contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:            # commit on success, roll back on error
                yield db
        finally:
            db.close()

    def processed(self, uidvalidity):
        """{uid: status} for this UIDVALIDITY."""
        with self._db() as db:
            rows = db.execute("SELECT uid, status FROM processed WHERE uidvalidity = ?",
                              (uidvalidity,)).fetchall()
        return dict(rows)

    def mark(self, uidvalidity, uids, status):
        """Mark UIDs as processed; returns how many were not marked before."""
        uids = sorted(set(uids))
        if not uids:
            return 0
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with self._db() as db:
            cur = db.executemany("INSERT OR IGNORE INTO processed VALUES (?, ?, ?, ?)",
                                 [(uidvalidity, uid, status, now) for uid in uids])
            return cur.rowcount
