#!/usr/bin/env python3
"""
Automatic configuration helper for PythOwnCloud.

Creates a .env file based on .env.example by prompting the user for values
or generating defaults.

The script performs no console output except for the prompts.

Author: Claude Code helper
"""
import os
import secrets
import hashlib

ENV_EXAMPLE = ".env.example"
ENV_FILE = ".env"

def read_template() -> str:
    with open(ENV_EXAMPLE, "r", encoding="utf-8") as f:
        return f.read()

def prompt(name: str, default: str | None = None) -> str:
    if default:
        prompt_text = f"{name} [{default}]: "
    else:
        prompt_text = f"{name}: "
    value = input(prompt_text).strip()
    return value or (default or "")

def generate_api_key() -> str:
    return secrets.token_urlsafe(32)

def hash_password(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()


def main() -> None:
    print("=== PythOwnCloud configuration helper ===")
    print("\nTemplate (.env.example):")
    print("-" * 40)
    print(read_template())
    print("-" * 40)

    api_key = prompt("POC_API_KEY", "REPLACE_WITH_RANDOM_API_KEY")
    if api_key == "REPLACE_WITH_RANDOM_API_KEY":
        api_key = generate_api_key()

    login_hash = prompt("POC_LOGIN_PASSWORD_HASH", "REPLACE_WITH_GENERATED_HASH")
    if login_hash == "REPLACE_WITH_GENERATED_HASH":
        pwd = prompt("Enter password for web UI / WebDAV")
        if not pwd:
            raise SystemExit("Password cannot be empty.")
        login_hash = hash_password(pwd)

    data_folder = prompt("POC_DATA_FOLDER", "/mnt/external-disk/pythowncloud-data")
    storage_path = prompt("POC_STORAGE_PATH", "/data")
    db_path_dir = prompt("POC_DB_PATH_DIR", "/var/lib/pythowncloud")

    session_ttl = prompt("POC_SESSION_TTL_DAYS", "7")
    session_secure = prompt("POC_SESSION_COOKIE_SECURE", "false")
    s3_access_key = prompt("POC_S3_ACCESS_KEY", "pythowncloud")
    s3_secret_key = prompt("POC_S3_SECRET_KEY", "REPLACE_WITH_RANDOM_S3_SECRET")
    if s3_secret_key == "REPLACE_WITH_RANDOM_S3_SECRET":
        s3_secret_key = secrets.token_hex(32)
    s3_region = prompt("POC_S3_REGION", "us-east-1")

    env_lines = [
        f"POC_API_KEY={api_key}",
        f"POC_LOGIN_PASSWORD_HASH={login_hash}",
        f"POC_DATA_FOLDER={data_folder}",
        f"POC_STORAGE_PATH={storage_path}",
        f"POC_DB_PATH_DIR={db_path_dir}",
        f"POC_SESSION_TTL_DAYS={session_ttl}",
        f"POC_SESSION_COOKIE_SECURE={session_secure}",
        f"POC_S3_ACCESS_KEY={s3_access_key}",
        f"POC_S3_SECRET_KEY={s3_secret_key}",
        f"POC_S3_REGION={s3_region}",
    ]

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(env_lines) + "\n")
    print(f"\n✅ .env written to {ENV_FILE}")

if __name__ == "__main__":
    main()
