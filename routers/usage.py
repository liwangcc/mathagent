"""用量查询 API — 查看当前老师的用量统计和限制规则。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from models_user import User
from routers.auth import _require_user
from services.rate_limiter import RateLimiter

router = APIRouter()


@router.get("/usage/stats")
def get_usage_stats(
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """查看今日学生图片用量及班级、学生限制。"""
    limiter = RateLimiter(db)
    return limiter.get_usage_stats(user.id)


@router.get("/usage/limits")
def get_limits():
    """查看限制规则说明（无需登录）。"""
    limiter = RateLimiter(None)
    return limiter.get_limits_info()


@router.get("/usage/check")
def check_usage(
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """快速检查当前是否可以继续批阅（不记录用量）。"""
    limiter = RateLimiter(db)
    daily = limiter.check_daily_image_limit(user.id)
    classes = limiter.check_class_limit(user.id)
    return {
        "can_grade": daily["allowed"],
        "daily_images": daily,
        "monthly_images": daily,
        "classes": classes,
    }
