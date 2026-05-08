# auth.py
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from database import supabase

# -----------------------------
# Config
# -----------------------------
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

# -----------------------------
# Dependency to get current doctor
# -----------------------------
async def get_current_doctor(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # Verify the token with Supabase
        res = supabase.auth.get_user(token)
        if not res.user:
            raise credentials_exception
        
        # Return user data (Supabase User object)
        return {
            "id": res.user.id,
            "email": res.user.email,
            "full_name": res.user.user_metadata.get("full_name", "Doctor"),
            "license_number": res.user.user_metadata.get("license_number", "N/A")
        }
    except Exception:
        raise credentials_exception
