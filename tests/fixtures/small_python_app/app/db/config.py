"""Database configuration module.

Reads the DATABASE_URL environment variable to locate the database.
"""
import os

DEFAULT_DATABASE_URL = "sqlite:///local.db"


def get_database_url():
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
