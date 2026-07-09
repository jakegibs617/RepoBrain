"""API routes for the user service."""
from app.handlers.user_handler import handle_create_user, handle_get_user


def register_routes(app):
    @app.route("/api/users", methods=["POST"])
    def create_user_route():
        return handle_create_user()

    @app.route("/api/users/<int:user_id>", methods=["GET"])
    def get_user_route(user_id):
        return handle_get_user(user_id)
