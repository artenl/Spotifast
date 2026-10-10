"""Server-side account administration (`spotifast-admin`).

Used to create the first admin account; everything else can then be done
from the /admin page.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from .config import Settings
from .db import Database
from .main import USERNAME
from .security import generate_temp_password, hash_password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spotifast-admin", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    create = sub.add_parser("create-user", help="create an account with a temporary password")
    create.add_argument("username")
    create.add_argument("--admin", action="store_true", help="give access to /admin")
    reset = sub.add_parser("reset-password", help="issue a new temporary password")
    reset.add_argument("username")
    sub.add_parser("list-users", help="list accounts")
    enable = sub.add_parser("set-active", help="enable or disable an account")
    enable.add_argument("username")
    enable.add_argument("state", choices=["on", "off"])
    args = parser.parse_args(argv)

    db = Database(Settings().db_path)

    if args.cmd == "list-users":
        for u in db.list_users():
            flags = ",".join(f for f, on in (("admin", u.is_admin), ("disabled", not u.is_active),
                                              ("must-change-password", u.must_change_password))
                             if on)
            print(f"{u.username}\t{flags}")
        return 0

    if args.cmd == "create-user":
        if not USERNAME.match(args.username):
            print("invalid username: 3-32 chars of letters, digits, . - _", file=sys.stderr)
            return 2
        password = generate_temp_password()
        try:
            db.create_user(args.username, hash_password(password), is_admin=args.admin)
        except sqlite3.IntegrityError:
            print(f"user {args.username!r} already exists", file=sys.stderr)
            return 1
        print(f"created {args.username}{' (admin)' if args.admin else ''}")
        print(f"temporary password: {password}")
        print("it must be changed at first login.")
        return 0

    user = db.get_user_by_name(args.username)
    if user is None:
        print(f"no such user: {args.username}", file=sys.stderr)
        return 1
    if args.cmd == "reset-password":
        password = generate_temp_password()
        db.set_password(user.id, hash_password(password), must_change=True)
        db.delete_user_sessions(user.id)
        print(f"temporary password for {user.username}: {password}")
        return 0
    if args.cmd == "set-active":
        db.set_active(user.id, args.state == "on")
        print(f"{user.username}: {'enabled' if args.state == 'on' else 'disabled'}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
