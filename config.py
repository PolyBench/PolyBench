import os
import sys
from dotenv import load_dotenv

# Force load solely from the release/ directory to guarantee standalone viability
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if not os.path.exists(env_path):
    print(f"\n[!] WARNING: No .env file found in the standalone directory ({env_path}).")
    print("[!] For standalone use, please copy your root .env file into the release/ folder.\n")

load_dotenv(dotenv_path=env_path) # Load strictly from local release path

# Polymarket Configuration
POLYGON_PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY")
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET")
POLYMARKET_API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE")



# Trading Configuration
MAX_TRADE_AMOUNT_USDC = float(os.getenv("MAX_TRADE_AMOUNT_USDC", "10.0"))


def validate_openrouter_api_key(api_key=None):
    """Validates the exact OpenRouter API keys via the official auth endpoint."""
    import requests
    key_to_check = api_key or os.getenv("OPENROUTER_API_KEY")
    print("Verifying OpenRouter API Key...")
    if not key_to_check:
        print("  [-] Error: OPENROUTER_API_KEY is not set.")
        return False
        
    headers = {
        "Authorization": f"Bearer {key_to_check}"
    }
    
    try:
        response = requests.get("https://openrouter.ai/api/v1/auth/key", headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json().get("data", {})
            label = data.get("label", "Unknown Key Label")
            limit = data.get("limit")
            usage = data.get("usage", 0)
            
            limit_str = f"${limit}" if limit is not None else "Unlimited"
            print(f"  [+] SUCCESS: Key is VALID.")
            print(f"      Label: {label}")
            print(f"      Usage: ${usage} / {limit_str}")
            return True
        elif response.status_code == 401:
            print("  [-] FAILED: Key is INVALID (401 Unauthorized). Check your .env definitions.")
            return False
        else:
            print(f"  [-] WARNING: Verification returned unexpected status ({response.status_code}): {response.text}")
            return False
    except Exception as e:
        print(f"  [-] ERROR: Could not connect to OpenRouter server: {e}")
        return False

# OpenRouter Configuration
PRIMARY_PROVIDER = os.getenv("PRIMARY_PROVIDER", "OPENROUTER")  # 'GOOGLE', 'XIAOMI', or 'OPENROUTER'
if not PRIMARY_PROVIDER:
    raise ValueError("PRIMARY_PROVIDER must be set")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not OPENROUTER_API_KEY:
    raise ValueError("OPENROUTER_API_KEY environment variable must be set")

if PRIMARY_PROVIDER == "OPENROUTER":
    if not validate_openrouter_api_key(OPENROUTER_API_KEY):
        print("[!] Critical Error: OpenRouter API validation failed. Aborting startup.")
        sys.exit(1)

OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL")
if not OPENROUTER_MODEL:
    raise ValueError("OPENROUTER_MODEL environment variable must be set")

OPENROUTER_PROVIDER_ORDER = os.getenv("OPENROUTER_PROVIDER_ORDER")
if not OPENROUTER_PROVIDER_ORDER:
    raise ValueError("OPENROUTER_PROVIDER_ORDER environment variable must be set")

# System Configuration
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
VERBOSE_MODE = os.getenv("VERBOSE_MODE", "true").lower() == "true"  # Show detailed output
NEWS_LIMIT = int(os.getenv("NEWS_LIMIT", "5"))  # Number of news articles to fetch

# Database configuration
DB_PATH = os.getenv('POLY_DB_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'database', 'polymarket_analysis.db'))
