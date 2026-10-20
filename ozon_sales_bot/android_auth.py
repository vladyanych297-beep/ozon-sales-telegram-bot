"""Local SQLite bridge between the existing bot and Android API (no polling here)."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def is_group_member(status: str, is_member: bool = False) -> bool:
    return status in {"creator", "administrator", "member"} or (
        status == "restricted" and is_member is True
    )


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def auth_scope(bot_token: str, group_id: int) -> str:
    return digest(f"{bot_token}:{group_id}")


class AuthStore:
    CHALLENGE_SECONDS = 300
    SESSION_SECONDS = 30 * 24 * 3600

    def __init__(self, path: Path, scope: str) -> None:
        self.path, self.scope = path, scope
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS android_challenges (
                    scope TEXT NOT NULL, id TEXT PRIMARY KEY, link_hash TEXT NOT NULL,
                    poll_hash TEXT NOT NULL, code TEXT NOT NULL, expires REAL NOT NULL,
                    user_id INTEGER, consumed INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS android_link ON android_challenges(link_hash);
                CREATE TABLE IF NOT EXISTS android_sessions (
                    scope TEXT NOT NULL, token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL, expires REAL NOT NULL
                );
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self) -> dict:
        now = time.time()
        challenge, link, poll = (secrets.token_urlsafe(32) for _ in range(3))
        code = f"{secrets.randbelow(1000000):06d}"
        with self._db() as db:
            db.execute("DELETE FROM android_challenges WHERE expires <= ?", (now,))
            db.execute("DELETE FROM android_sessions WHERE expires <= ?", (now,))
            if db.execute("SELECT COUNT(*) FROM android_challenges").fetchone()[0] >= 1000:
                raise OverflowError("Слишком много запросов входа. Повторите позже.")
            db.execute(
                "INSERT INTO android_challenges "
                "(scope,id,link_hash,poll_hash,code,expires) VALUES (?,?,?,?,?,?)",
                (self.scope, challenge, digest(link), digest(poll), code,
                 now + self.CHALLENGE_SECONDS),
            )
        return {"challenge_id": challenge, "link_token": link, "poll_secret": poll,
                "code": code, "expires_in": self.CHALLENGE_SECONDS}

    def preview(self, link: str) -> str | None:
        with self._db() as db:
            row = db.execute(
                "SELECT code FROM android_challenges WHERE scope=? AND link_hash=? "
                "AND expires>? AND user_id IS NULL AND consumed=0",
                (self.scope, digest(link), time.time()),
            ).fetchone()
        return row["code"] if row else None

    def approve(self, link: str, user_id: int) -> bool:
        if user_id <= 0:
            return False
        with self._db() as db:
            result = db.execute(
                "UPDATE android_challenges SET user_id=? WHERE scope=? AND link_hash=? "
                "AND expires>? AND user_id IS NULL AND consumed=0",
                (user_id, self.scope, digest(link), time.time()),
            )
        return result.rowcount == 1

    def poll_user(self, challenge: str, poll: str) -> int | None:
        with self._db() as db:
            row = db.execute(
                "SELECT user_id FROM android_challenges WHERE scope=? AND id=? "
                "AND poll_hash=? AND expires>? AND consumed=0",
                (self.scope, challenge, digest(poll), time.time()),
            ).fetchone()
        if row is None:
            raise ValueError("Запрос входа истёк или уже использован. Начните вход заново.")
        return row["user_id"]

    def exchange(self, challenge: str, poll: str, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        with self._db() as db:
            changed = db.execute(
                "UPDATE android_challenges SET consumed=1 WHERE scope=? AND id=? "
                "AND poll_hash=? AND expires>? AND consumed=0 AND user_id=?",
                (self.scope, challenge, digest(poll), time.time(), user_id),
            )
            if changed.rowcount != 1:
                raise ValueError("Запрос входа истёк или уже использован. Начните вход заново.")
            db.execute("INSERT INTO android_sessions VALUES (?,?,?,?)",
                       (self.scope, digest(token), user_id, time.time() + self.SESSION_SECONDS))
        return token

    def user(self, token: str) -> int | None:
        with self._db() as db:
            row = db.execute(
                "SELECT user_id FROM android_sessions WHERE scope=? AND token_hash=? AND expires>?",
                (self.scope, digest(token), time.time()),
            ).fetchone()
        return row["user_id"] if row else None

    def revoke(self, token: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM android_sessions WHERE scope=? AND token_hash=?",
                       (self.scope, digest(token)))

    def revoke_user(self, user_id: int) -> None:
        with self._db() as db:
            db.execute("DELETE FROM android_sessions WHERE scope=? AND user_id=?",
                       (self.scope, user_id))
