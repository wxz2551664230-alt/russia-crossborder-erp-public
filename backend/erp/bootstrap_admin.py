"""Initialize the first shared-server administrator after database migration."""
import argparse
from getpass import getpass

from erp.core.database import SessionLocal
from erp.services.accounts import create_user


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化首个 ERP 管理员（密码通过隐藏输入提供）")
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", default="管理员")
    args = parser.parse_args()
    password = getpass("密码（至少10个字符）: ")
    if password != getpass("再次输入密码: "):
        parser.error("两次密码不一致")
    with SessionLocal() as db:
        create_user(db, username=args.username, display_name=args.display_name, password=password, role="admin", bootstrap=True)
    print("管理员已创建。确认迁移完成后，启用 ERP_MULTIUSER_ENABLED 并重启服务。")


if __name__ == "__main__":
    main()
