import jwt
from fastapi import HTTPException, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from uuid import UUID
import os
from dotenv import load_dotenv
import jwt
from typing import List, Optional
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, ValidationError, field_validator
from core.config import settings

load_dotenv()

security = HTTPBearer()

JWT_SECRET = os.getenv("JWT_SECRET") 
JWT_ALGORITHM = "HS256"

def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> dict:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        
        # Verify scope/permission
        scopes = payload.get("scopes", [])
        if "order.read.own" not in scopes:
            raise HTTPException(status_code=403, detail="Not enough permissions: order.read.own required")
            
        return payload
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired authentication token")

security_bearer = HTTPBearer()

class CurrentUser(BaseModel):
    id: int = Field(..., alias="user_id")
    role: str = Field(..., alias="role")
    permissions: List[str]

    @field_validator("permissions", mode="before")
    @classmethod
    def extract_permission_codes(cls, v):
        return [item["code"] if isinstance(item, dict) else item for item in v]               # JWT'dagi 'permissions[]'

def get_current_user(credentials: HTTPAuthorizationCredentials = Security(security_bearer)) -> CurrentUser:
    token = credentials.credentials
    try:
        payload = jwt.decode(
            token, 
            settings.jwt_secret, 
            algorithms=["HS256"],
            issuer="foodexpress-auth"
        )
        return CurrentUser(**payload)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token muddati tugagan")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Yaroqsiz token")
    except ValidationError as e:
        missing = [err["loc"][0] for err in e.errors() if err["type"] == "missing"]
        detail = f"Token tarkibida yetishmayotgan maydonlar: {missing}" if missing else "Token tarkibi noto'g'ri"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)

class PermissionChecker:
    def __init__(self, required_permission: str):
        self.required_permission = required_permission

    def __call__(self, current_user: CurrentUser = Security(get_current_user)) -> CurrentUser:
        # 2. Matritsadagi permission foydalanuvchida borligini tekshirish
        if self.required_permission not in current_user.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Tizimga kirish taqiqlangan. Yetishmayotgan huquq: {self.required_permission}"
            )
        return current_user