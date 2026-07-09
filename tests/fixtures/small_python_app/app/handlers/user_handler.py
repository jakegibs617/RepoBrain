"""HTTP handlers translating requests into service calls."""
from app.services.user_service import create_user, get_user


def handle_create_user():
    payload = {"name": "example"}
    user = create_user(payload)
    return {"status": "created", "user": user}


def handle_get_user(user_id):
    user = get_user(user_id)
    return {"status": "ok", "user": user}
