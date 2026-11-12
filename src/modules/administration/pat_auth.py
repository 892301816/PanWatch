"""交易同步端点的鉴权:登录 JWT 或带 ``trade:write`` scope 的 PAT。

手机端自动化（MacroDroid/Tasker/AutoJs）没有登录会话，用 PAT 最合适；
网页设置页仍然用登录 JWT 调同样的接口，避免多一套令牌。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from src.modules.administration.api.auth import verify_token
from src.modules.administration.pat import (
    SCOPE_TRADE_WRITE,
    hash_token,
    looks_like_pat,
    parse_scopes,
)
from src.platform.persistence.database import get_db
from src.platform.persistence.models import PersonalAccessToken

logger = logging.getLogger(__name__)
_security = HTTPBearer(auto_error=False)


def authenticate_sync(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
    db: Session = Depends(get_db),
) -> dict:
    """返回 ``{"kind": "pat"|"jwt", "pat": row|None}``;失败抛 401/403。"""
    token = credentials.credentials if credentials else ""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if looks_like_pat(token):
        row = (
            db.query(PersonalAccessToken)
            .filter(PersonalAccessToken.token_hash == hash_token(token))
            .first()
        )
        if row is None or row.revoked_at is not None:
            raise HTTPException(401, "同步令牌无效或已吊销")
        if row.expires_at is not None and row.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
            raise HTTPException(401, "同步令牌已过期")
        if SCOPE_TRADE_WRITE not in parse_scopes(row.scopes_json):
            raise HTTPException(403, "该令牌没有 trade:write 权限")
        row.last_used_at = datetime.now(timezone.utc).replace(tzinfo=None)
        row.last_used_ip = request.client.host if request.client else None
        db.commit()
        return {"kind": "pat", "pat": row}

    if not verify_token(token):
        raise HTTPException(401, "登录已过期，请重新登录")
    return {"kind": "jwt", "pat": None}
