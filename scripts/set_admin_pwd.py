"""显式重置 MathAgent 管理员密码。

背景：启动流程现在**只在管理员不存在或没有密码时**写入初始密码，已有密码不会被
.env 覆盖。这样做的好处是老师在界面上改的密码重启后不会回滚；代价是「忘记密码」
需要一个显式入口——就是这个脚本。

用法（在仓库根目录 / 容器内）：

    # 交互式：列出所有账号，选一个并输入新密码（推荐）
    python scripts/set_admin_pwd.py

    # 直接指定手机号与新密码（脚本不会回显密码）
    python scripts/set_admin_pwd.py --phone 13800138000 --password 'Abc123456'

    # 只查看当前账号，不做修改
    python scripts/set_admin_pwd.py --list

    # 把某个已有教师提升为管理员
    python scripts/set_admin_pwd.py --phone 13900139000 --promote

数据库位置解析顺序与后端一致：DATABASE_URL（sqlite:///...）→
MATHAGENT_DATA_DIR/mathagent.db → 仓库根目录 mathagent.db。

脚本不读取 .env，不会打印明文密码，也不会回写任何配置文件。
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
_PWD_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).{6,20}$")


def _db_path() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    data_dir = os.environ.get("MATHAGENT_DATA_DIR", "").strip()
    if data_dir:
        return str(Path(data_dir) / "mathagent.db")
    return str(_ROOT / "mathagent.db")


def _hash_password(password: str) -> str:
    """优先用应用自己的实现，保证与登录校验完全一致。"""
    try:
        sys.path.insert(0, str(_ROOT))
        from services.auth_service import hash_password  # noqa: E402

        return hash_password(password)
    except Exception:
        import bcrypt

        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _connect() -> sqlite3.Connection:
    path = _db_path()
    if not Path(path).is_file():
        raise SystemExit(f"找不到数据库：{path}")
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _rows(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT id, phone, name, role, status, password_hash FROM users ORDER BY id"
    ).fetchall()


def _print_users(conn: sqlite3.Connection) -> None:
    rows = _rows(conn)
    if not rows:
        print("数据库里还没有任何账号。")
        return
    print(f"{'ID':>4}  {'手机号/账号':<13} {'姓名':<12} {'角色':<8} {'密码'}")
    for uid, phone, name, role, _status, pwd_hash in rows:
        print(
            f"{uid:>4}  {str(phone or '-'):<13} {str(name or ''):<12} "
            f"{str(role or ''):<8} {'已设置' if pwd_hash else '未设置'}"
        )


def _pick_admin(conn: sqlite3.Connection) -> tuple | None:
    rows = [r for r in _rows(conn) if (r[3] or "").lower() == "admin"]
    return rows[0] if rows else None


def _reset(conn: sqlite3.Connection, uid: int, password: str, promote: bool) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    new_hash = _hash_password(password)
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, uid)
    )
    if promote:
        conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (uid,))
    conn.commit()
    verified = conn.execute(
        "SELECT password_hash FROM users WHERE id = ?", (uid,)
    ).fetchone()
    if not verified or verified[0] != new_hash:
        raise SystemExit("写入校验失败：数据库中的密码哈希与预期不一致")
    print(f"ok user_id={uid} promote={bool(promote)} changed_at={stamp}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重置 MathAgent 管理员密码")
    parser.add_argument("--phone", help="目标账号的手机号/登录账号")
    parser.add_argument("--password", help="新密码（6-20 位，含字母和数字）；省略则交互输入")
    parser.add_argument("--promote", action="store_true", help="把目标账号提升为管理员")
    parser.add_argument("--list", action="store_true", help="只列出账号，不修改")
    args = parser.parse_args(argv)

    conn = _connect()
    try:
        if args.list:
            _print_users(conn)
            return 0

        if args.phone:
            phone = args.phone.strip()
            if not _PHONE_RE.match(phone):
                raise SystemExit("手机号格式错误：需为 11 位大陆手机号或 199 开头的登录账号")
            row = conn.execute(
                "SELECT id, password_hash FROM users WHERE phone = ?", (phone,)
            ).fetchone()
            if row is None:
                raise SystemExit(f"没有找到手机号为 {phone} 的账号")
            uid = row[0]
        else:
            admin = _pick_admin(conn)
            if admin is None:
                _print_users(conn)
                raise SystemExit(
                    "还没有管理员账号：请用 --phone 指定一个已有账号并加 --promote"
                )
            uid = admin[0]
            print(f"目标管理员：id={admin[0]} phone={admin[1]} name={admin[2]}")

        password = args.password
        if not password:
            password = getpass.getpass("请输入新密码（不显示）：")
            confirm = getpass.getpass("请再次输入新密码：")
            if password != confirm:
                raise SystemExit("两次输入的密码不一致")
        if not _PWD_RE.match(password):
            raise SystemExit("密码需 6-20 位，且至少包含字母和数字")

        _reset(conn, uid, password, promote=args.promote)
        print("密码已更新。请用新密码在登录页登录；启动流程不会再覆盖它。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())