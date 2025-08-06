from fastapi import FastAPI, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from database import SessionLocal
from models import User
import secrets
from pydantic import BaseModel, Field
from enum import Enum

app = FastAPI()

class Tier(str, Enum):
    free = "free"
    silver = "silver"
    gold = "gold"

class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    tier: Tier = Tier.free
    quota: int = 1

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def require_admin(x_admin_key: str = Header(...)):
    if x_admin_key != "YOUR_ADMIN_SECRET":  # <-- Set your real admin key here
        raise HTTPException(status_code=403, detail="Forbidden")
    return True

@app.post("/users")
def create_user(
    req: UserCreateRequest,
    db: Session = Depends(get_db),
    admin_ok: bool = Depends(require_admin)
):
    if db.query(User).filter_by(username=req.username).first():
        raise HTTPException(status_code=400, detail="Username exists")
    api_key = secrets.token_hex(16)
    user = User(username=req.username, api_key=api_key, tier=req.tier, quota=req.quota)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {
        "username": user.username,
        "api_key": user.api_key,
        "tier": user.tier,
        "quota": user.quota
    }
