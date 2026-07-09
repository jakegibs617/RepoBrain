"""Business logic for users."""
from app.repositories.user_repository import UserRepository

_repo = UserRepository()


def create_user(payload):
    if not payload.get("name"):
        raise ValueError("name is required")
    return _repo.insert(payload)


def get_user(user_id):
    return _repo.find_by_id(user_id)
