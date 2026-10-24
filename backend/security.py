"""Demo sign-in and data filtering. Localhost demo only; not a secure login system.

Tokens come from data/demo/users.json and are sent as `Authorization: Bearer <token>` or
`?token=<token>`. Roles:
  operator   everything, including approving plans and setting the horizon
  committee  rescue queue, accepting jobs, recording arrivals; sees assistance details
  resident   only their own household's plan
  public     (no token) the dashboard with medical and personal details removed
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROLE_RANK = {"public": 0, "resident": 1, "committee": 2, "operator": 3}


class Forbidden(PermissionError):
    pass


@dataclass(frozen=True)
class User:
    token: Optional[str]
    role: str
    name: str
    household_id: Optional[str] = None


PUBLIC = User(None, "public", "public")


class Auth:
    def __init__(self, users_file: Path):
        data = json.loads(Path(users_file).read_text())
        self.users = {u["token"]: User(u["token"], u["role"], u.get("name", u["role"]), u.get("household_id"))
                      for u in data.get("users", [])}

    def identify(self, token: Optional[str]) -> User:
        if not token:
            return PUBLIC
        user = self.users.get(token)
        if user is None:
            raise Forbidden("unknown token")
        return user


def require(user: User, *roles: str) -> None:
    if user.role not in roles:
        raise Forbidden(f"this needs role {' or '.join(roles)}; you are {user.role}")


def can_see_details(user: User) -> bool:
    """Assistance needs, essentials and names: committee and operator only."""
    return user.role in ("committee", "operator")


def household_scope(user: User, household_id: str) -> None:
    """Residents may only touch their own household; committee/operator may touch any."""
    if user.role == "resident" and user.household_id != household_id:
        raise Forbidden("residents can only see or update their own household")
    if user.role == "public":
        raise Forbidden("sign in required")


SENSITIVE_KEYS = {"assistance", "checklist", "essentials", "stated_needs", "name", "contact", "age_band", "mobility", "message"}


def strip_sensitive(obj):
    """Recursively remove personal and medical fields (for the public view)."""
    if isinstance(obj, dict):
        return {k: strip_sensitive(v) for k, v in obj.items() if k not in SENSITIVE_KEYS}
    if isinstance(obj, list):
        return [strip_sensitive(v) for v in obj]
    return obj
