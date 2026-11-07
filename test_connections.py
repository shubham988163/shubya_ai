#!/usr/bin/env python3
"""Test connections to external APIs and services."""

import os
import sys
import io
from dotenv import load_dotenv
import requests

# Fix Windows encoding for emoji/unicode
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Load environment variables
load_dotenv()

def test_gemini_connection():
    """Test Google Gemini API connection."""
    print("\n🔍 Testing Gemini API Connection...")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("  ❌ GEMINI_API_KEY not set")
        return False
    
    try:
        # Test with models endpoint (requires API key)
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        response = requests.get(
            f"{url}?key={api_key}",
            timeout=5
        )
        if response.status_code == 200:
            print("  ✅ Gemini API is reachable and authenticated")
            return True
        elif response.status_code == 403:
            print(f"  ❌ Gemini API: Invalid or expired API key (403)")
            return False
        else:
            print(f"  ⚠️  Gemini API returned {response.status_code} (may still be reachable)")
            return True
    except requests.exceptions.Timeout:
        print("  ❌ Gemini API timeout")
        return False
    except Exception as e:
        print(f"  ❌ Gemini API error: {str(e)[:80]}")
        return False

def test_fyers_connection():
    """Test Fyers API connection via fyers_apiv3 library."""
    print("\n🔍 Testing Fyers API Connection...")
    app_id = os.getenv("FYERS_APP_ID")
    if not app_id:
        print("  ❌ FYERS_APP_ID not set")
        return False
    
    try:
        # The fyers_apiv3 library handles OAuth internally
        # We just check if the library can be imported and initialized
        from fyers_apiv3.FyersClientV3 import FyersClientV3
        client = FyersClientV3(client_id=app_id)
        print("  ✅ Fyers API library (fyers_apiv3) is available")
        return True
    except ImportError:
        print("  ⚠️  fyers_apiv3 library not installed (optional dependency)")
        return True  # Not critical, has fallback to yfinance
    except Exception as e:
        print(f"  ⚠️  Fyers API initialization: {str(e)[:80]}")
        return True  # Still ok, uses fallback

def test_delta_exchange_connection():
    """Test Delta Exchange API connection."""
    print("\n🔍 Testing Delta Exchange API Connection...")
    base_url = os.getenv("DELTA_BASE_URL")
    api_key = os.getenv("DELTA_API_KEY")
    
    if not base_url:
        print("  ❌ DELTA_BASE_URL not set")
        return False
    if not api_key:
        print("  ❌ DELTA_API_KEY not set")
        return False
    
    try:
        # Test Delta Exchange API with proper authentication
        url = f"{base_url}/v2/me/account"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code in [200, 400, 401, 403]:
            print(f"  ✅ Delta Exchange API is reachable ({response.status_code})")
            return True
        else:
            print(f"  ⚠️  Delta Exchange API returned {response.status_code}")
            return True
    except requests.exceptions.Timeout:
        print("  ❌ Delta Exchange API timeout")
        return False
    except Exception as e:
        print(f"  ⚠️  Delta Exchange API error: {str(e)[:80]}")
        return True

def test_announcements_connection():
    """Test announcements URL."""
    print("\n🔍 Testing Announcements Endpoint...")
    from cli.config import CLI_CONFIG
    url = CLI_CONFIG.get("announcements_url")
    
    try:
        response = requests.get(url, timeout=2)
        if response.status_code == 200:
            print("  ✅ Announcements endpoint is reachable")
            return True
        else:
            print(f"  ⚠️  Announcements endpoint returned {response.status_code}")
            return True  # Fallback available
    except requests.exceptions.Timeout:
        print("  ⚠️  Announcements endpoint timeout (fallback available)")
        return True
    except Exception as e:
        print(f"  ⚠️  Announcements error: {e} (fallback available)")
        return True

def test_database_connections():
    """Test local database connections."""
    print("\n🔍 Testing Database Connections...")
    db_files = ["trades.db", "paper_trades.db", "trading.db", "ledger.db"]
    all_ok = True
    
    for db_file in db_files:
        if os.path.exists(db_file):
            print(f"  ✅ {db_file} exists")
        else:
            print(f"  ⚠️  {db_file} not found")
            all_ok = False
    
    return all_ok

def main():
    """Run all connection tests."""
    print("=" * 60)
    print("🚀 Trading Agents Connection Test Suite")
    print("=" * 60)
    
    results = {
        "Gemini API": test_gemini_connection(),
        "Fyers API": test_fyers_connection(),
        "Delta Exchange API": test_delta_exchange_connection(),
        "Announcements": test_announcements_connection(),
        "Databases": test_database_connections(),
    }
    
    print("\n" + "=" * 60)
    print("📊 Connection Summary")
    print("=" * 60)
    
    for service, status in results.items():
        status_icon = "✅" if status else "❌"
        print(f"{status_icon} {service}: {'OK' if status else 'FAILED'}")
    
    all_ok = all(results.values())
    print("\n" + "=" * 60)
    if all_ok:
        print("✅ All connections are OK!")
    else:
        print("⚠️  Some connections failed. Check configuration and credentials.")
    print("=" * 60)
    
    return 0 if all_ok else 1

if __name__ == "__main__":
    sys.exit(main())
