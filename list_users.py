# list_users.py

from database import SessionLocal
from models import User

db = SessionLocal()
users = db.query(User).all()

if not users:
    print("No users found.")
else:
    print(f"{'ID':<3} {'Username':<20} {'API Key':<36} {'Tier':<10} {'Quota'}")
    print("-" * 80)
    for user in users:
        print(f"{user.id:<3} {user.username:<20} {user.api_key:<36} {user.tier:<10} {user.quota}")
db.close()
