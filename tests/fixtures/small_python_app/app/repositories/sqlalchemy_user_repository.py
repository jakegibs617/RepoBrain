"""Representative SQLAlchemy calls; the fixture is indexed, not imported."""
from app.models.user import User


class SqlAlchemyUserRepository:
    def load_by_id(self, session, user_id):
        return session.get(User, user_id)

    def persist(self, session, payload):
        session.add(User(**payload))
