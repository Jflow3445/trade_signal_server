# create_user.py
import secrets
from database import SessionLocal
from models import User

db = SessionLocal()
api_key = secrets.token_hex(16)
user = User(username="gold_user", api_key=api_key, tier="gold", quota=9999)
db.add(user)
db.commit()
print("API Key:", api_key)
db.close()
