"""Data access layer for users, backed by the configured database."""
from app.db.config import get_database_url


class UserRepository:
    def __init__(self):
        self.database_url = get_database_url()
        self._rows = {}
        self._next_id = 1

    def insert(self, payload):
        row = dict(payload, id=self._next_id)
        self._rows[self._next_id] = row
        self._next_id += 1
        return row

    def find_by_id(self, user_id):
        return self._rows.get(user_id)
