#!/usr/bin/env python3
"""
Diagnostic script to troubleshoot backend configuration issues.
This helps identify why Cloudflare backend might not be working.
"""

import os
import sys
from pathlib import Path

# Add src to path for imports
src_path = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(src_path))

def print_separator(title):
    print("\n" + "=" * 60)
    print(f" {title}")
    print("=" * 60)

def print_status(status, message):
    """Print status with simple text indicators."""
    if status == "success":
        print(f"[OK] {message}")
    elif status == "warning":
        print(f"[WARN] {message}")
    elif status == "error":
        print(f"[ERROR] {message}")
    else:
        print(f"[INFO] {message}")

def check_env_file():
    """Check if .env file exists and what it contains."""
    print_separator("ENVIRONMENT FILE CHECK")

    project_root = Path(__file__).parent.parent.parent
    env_file = project_root / ".env"

    if env_file.exists():
        print_status("success", f".env file found at: {env_file}")
        print("\n.env file contents:")
        with open(env_file, 'r') as f:
            lines = f.readlines()
            for i, line in enumerate(lines, 1):
                # Mask sensitive values
                if 'TOKEN' in line or 'PASSWORD' in line or 'SECRET' in line:
                    if '=' in line:
                        key, _ = line.split('=', 1)
                        print(f"  {i:2d}: {key}=***MASKED***")
                    else:
                        print(f"  {i:2d}: {line.rstrip()}")
                else:
                    print(f"  {i:2d}: {line.rstrip()}")
    else:
        print_status("error", f"No .env file found at: {env_file}")
        return False
    return True

def check_environment_variables():
    """Check current environment variables."""
    print_separator("ENVIRONMENT VARIABLES CHECK")

    # Check if dotenv is available and load .env file
    try:
        from dotenv import load_dotenv
        project_root = Path(__file__).parent.parent.parent
        env_file = project_root / ".env"
        if env_file.exists():
            load_dotenv(env_file)
            print_status("success", f"Loaded .env file from: {env_file}")
        else:
            print_status("info", "No .env file to load")
    except ImportError:
        print_status("warning", "dotenv not available, skipping .env file loading")

    # Core configuration
    storage_backend = os.getenv('MCP_MEMORY_STORAGE_BACKEND', 'NOT SET')
    print(f"\nCore Configuration:")
    print(f"  MCP_MEMORY_STORAGE_BACKEND: {storage_backend}")

    # Cloudflare variables
    cloudflare_vars = {
        'CLOUDFLARE_API_TOKEN': 'REQUIRED',
        'CLOUDFLARE_ACCOUNT_ID': 'REQUIRED',
        'CLOUDFLARE_VECTORIZE_INDEX': 'REQUIRED',
        'CLOUDFLARE_D1_DATABASE_ID': 'REQUIRED',
        'CLOUDFLARE_R2_BUCKET': 'OPTIONAL',
        'CLOUDFLARE_EMBEDDING_MODEL': 'OPTIONAL',
        'CLOUDFLARE_LARGE_CONTENT_THRESHOLD': 'OPTIONAL',
        'CLOUDFLARE_MAX_RETRIES': 'OPTIONAL',
        'CLOUDFLARE_BASE_DELAY': 'OPTIONAL'
    }

    print(f"\nCloudflare Configuration:")
    missing_required = []
    for var, requirement in cloudflare_vars.items():
        value = os.getenv(var)
        if value:
            if 'TOKEN' in var:
                display_value = f"{value[:8]}***MASKED***"
            else:
                display_value = value
            print_status("success", f"{var}: {display_value} ({requirement})")
        else:
            display_value = "NOT SET"
            if requirement == 'REQUIRED':
                print_status("error", f"{var}: {display_value} ({requirement})")
                missing_required.append(var)
            else:
                print_status("warning", f"{var}: {display_value} ({requirement})")

    if missing_required:
        print_status("error", f"Missing required Cloudflare variables: {', '.join(missing_required)}")
        return False
    else:
        print_status("success", "All required Cloudflare variables are set")
        return True

def test_config_import():
    """Test importing the configuration module."""
    print_separator("CONFIGURATION MODULE TEST")

    try:
        print("Attempting to import config module...")
        from mcp_memory_service.config import (
            STORAGE_BACKEND,
            CLOUDFLARE_API_TOKEN,
            CLOUDFLARE_ACCOUNT_ID,
            CLOUDFLARE_VECTORIZE_INDEX,
            CLOUDFLARE_D1_DATABASE_ID
        )

        print_status("success", "Config import successful")
        print(f"  Configured Backend: {STORAGE_BACKEND}")
        print(f"  API Token Set: {'YES' if CLOUDFLARE_API_TOKEN else 'NO'}")
        print(f"  Account ID: {CLOUDFLARE_ACCOUNT_ID}")
        print(f"  Vectorize Index: {CLOUDFLARE_VECTORIZE_INDEX}")
        print(f"  D1 Database ID: {CLOUDFLARE_D1_DATABASE_ID}")

        return STORAGE_BACKEND

    except SystemExit as e:
        print_status("error", f"Config import failed with SystemExit: {e}")
        print("   This means required Cloudflare variables are missing")
        return None
    except Exception as e:
        print_status("error", f"Config import failed with error: {e}")
        return None

def test_storage_creation():
    """Test creating the storage backend."""
    print_separator("STORAGE BACKEND CREATION TEST")

    try:
        from mcp_memory_service.config import STORAGE_BACKEND
        print(f"Attempting to create {STORAGE_BACKEND} storage...")

        if STORAGE_BACKEND == 'cloudflare':
            from mcp_memory_service.storage.cloudflare import CloudflareStorage
            from mcp_memory_service.config import (
                CLOUDFLARE_API_TOKEN,
                CLOUDFLARE_ACCOUNT_ID,
                CLOUDFLARE_VECTORIZE_INDEX,
                CLOUDFLARE_D1_DATABASE_ID,
                CLOUDFLARE_R2_BUCKET,
                CLOUDFLARE_EMBEDDING_MODEL,
                CLOUDFLARE_LARGE_CONTENT_THRESHOLD,
                CLOUDFLARE_MAX_RETRIES,
                CLOUDFLARE_BASE_DELAY
            )

            storage = CloudflareStorage(
                api_token=CLOUDFLARE_API_TOKEN,
                account_id=CLOUDFLARE_ACCOUNT_ID,
                vectorize_index=CLOUDFLARE_VECTORIZE_INDEX,
                d1_database_id=CLOUDFLARE_D1_DATABASE_ID,
                r2_bucket=CLOUDFLARE_R2_BUCKET,
                embedding_model=CLOUDFLARE_EMBEDDING_MODEL,
                large_content_threshold=CLOUDFLARE_LARGE_CONTENT_THRESHOLD,
                max_retries=CLOUDFLARE_MAX_RETRIES,
                base_delay=CLOUDFLARE_BASE_DELAY
            )
            print_status("success", "CloudflareStorage instance created successfully")
            print(f"  Storage class: {storage.__class__.__name__}")
            return storage

        elif STORAGE_BACKEND == 'sqlite_vec':
            from mcp_memory_service.storage.sqlite_vec import SqliteVecMemoryStorage
            from mcp_memory_service.config import SQLITE_VEC_PATH
            storage = SqliteVecMemoryStorage(SQLITE_VEC_PATH)
            print_status("success", "SqliteVecMemoryStorage instance created successfully")
            print(f"  Storage class: {storage.__class__.__name__}")
            print(f"  Database path: {SQLITE_VEC_PATH}")
            return storage

        else:
            print_status("error", f"Unknown storage backend: {STORAGE_BACKEND}")
            return None

    except Exception as e:
        print_status("error", f"Storage creation failed: {e}")
        import traceback
        print(f"Full traceback:")
        traceback.print_exc()
        return None

def main():
    """Run all diagnostic tests."""
    print("MCP Memory Service Backend Configuration Diagnostics")
    print("=" * 60)

    # Step 1: Check .env file
    check_env_file()

    # Step 2: Check environment variables
    cloudflare_ready = check_environment_variables()

    # Step 3: Test config import
    configured_backend = test_config_import()

    # Step 4: Test storage creation if config loaded successfully
    if configured_backend:
        storage = test_storage_creation()
    else:
        storage = None

    # Final summary
    print_separator("DIAGNOSTIC SUMMARY")

    if configured_backend == 'cloudflare' and cloudflare_ready and storage:
        print_status("success", "Cloudflare backend should be working correctly")
        print(f"   Configuration loaded: {configured_backend}")
        print(f"   Required variables set: {cloudflare_ready}")
        print(f"   Storage instance created: {storage.__class__.__name__}")
    elif configured_backend == 'sqlite_vec' and storage:
        print_status("success", "SQLite-vec backend is working")
        print(f"   Configuration loaded: {configured_backend}")
        print(f"   Storage instance created: {storage.__class__.__name__}")
        if cloudflare_ready:
            print_status("warning", "Cloudflare variables are set but backend is sqlite_vec")
            print("         Check MCP_MEMORY_STORAGE_BACKEND environment variable")
    else:
        print_status("error", "Backend configuration has issues")
        print(f"   Configured backend: {configured_backend or 'FAILED TO LOAD'}")
        print(f"   Cloudflare variables ready: {cloudflare_ready}")
        print(f"   Storage created: {'YES' if storage else 'NO'}")

        print("\nTROUBLESHOOTING STEPS:")
        if not cloudflare_ready:
            print("   1. Set missing Cloudflare environment variables")
            print("   2. Create .env file with Cloudflare credentials")
        if not configured_backend:
            print("   3. Fix environment variable loading issues")
        if configured_backend and not storage:
            print("   4. Check Cloudflare credentials and connectivity")

if __name__ == "__main__":
    main()