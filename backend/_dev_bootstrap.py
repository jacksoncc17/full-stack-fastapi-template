"""Dev bootstrap: create a user, a worker with a known API key, and print them.

Used to exercise the API with curl before the UI exists.

    python _dev_bootstrap.py
"""

import os
import sys
import uuid

from sqlmodel import SQLModel, Session, create_engine, select

from app import crud_jobs as cj
from app.models import User
from app.models_jobs import Worker

URL = os.environ["DATABASE_URL"]


def main():
    engine = create_engine(URL)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        email = "dev@example.com"
        user = s.exec(select(User).where(User.email == email)).first()
        if user is None:
            user = User(email=email, hashed_password="unused-dev", is_active=True)
            s.add(user)
            s.commit()
            s.refresh(user)

        key = "dev-worker-key"
        worker = s.exec(select(Worker).where(Worker.name == "dev-worker")).first()
        if worker is None:
            worker = Worker(name="dev-worker", api_key_hash=cj.hash_api_key(key))
            s.add(worker)
            s.commit()
            s.refresh(worker)

        print(f"USER_ID={user.id}")
        print(f"USER_EMAIL={email}")
        print(f"WORKER_ID={worker.id}")
        print(f"WORKER_KEY={key}")
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
