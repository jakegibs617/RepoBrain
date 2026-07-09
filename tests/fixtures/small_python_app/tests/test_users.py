"""Tests for the user service fixture app."""
from app.services.user_service import create_user, get_user


def test_create_user():
    user = create_user({"name": "ada"})
    assert user["id"] >= 1


def test_get_missing_user():
    assert get_user(999999) is None
