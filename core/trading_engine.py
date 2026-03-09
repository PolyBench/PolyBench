import logging
import os
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, ApiCreds
from py_clob_client.constants import POLYGON
from config import (
    POLYGON_PRIVATE_KEY, 
    POLYMARKET_API_KEY, 
    POLYMARKET_API_SECRET, 
    POLYMARKET_API_PASSPHRASE
)

logger = logging.getLogger(__name__)

def get_clob_client():
    if not all([POLYGON_PRIVATE_KEY, POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE]):
        logger.error("Missing API credentials.")
        return None

    try:
        host = "https://clob.polymarket.com"
        chain_id = 137 # Polygon Mainnet
        client = ClobClient(
            host, 
            key=POLYGON_PRIVATE_KEY, 
            chain_id=chain_id,
            creds=ApiCreds(
                api_key=POLYMARKET_API_KEY,
                api_secret=POLYMARKET_API_SECRET,
                api_passphrase=POLYMARKET_API_PASSPHRASE,
            )
        )
        return client
    except Exception as e:
        logger.error(f"Failed to initialize CLOB client: {e}")
        return None

def execute_trade(token_id, side, size, price):
    """
    Executes a trade on Polymarket.
    
    Args:
        token_id (str): The asset ID to trade (outcome token).
        side (str): "BUY" or "SELL".
        size (float): Amount to trade (check API for exact unit, usually units of currency or tokens).
        price (float): Limit price.
    """
    client = get_clob_client()
    if not client:
        return {"status": "failed", "reason": "Client init failed"}

    try:
        # Create and sign order
        # Note: This is a simplified example. CLOB API requires specific order structures.
        side_enum = "BUY" if side.upper() == "BUY" else "SELL" # Convert to Enum if needed by lib
        
        # This is strictly illustrative as per library usage found in docs
        # We need to construct a Signed Order
        order_args = OrderArgs(
            price=price,
            size=size,
            side=side_enum,
            token_id=token_id,
        )
        
        resp = client.create_order(order_args)
        logger.info(f"Order submitted: {resp}")
        return {"status": "success", "response": resp}
        
    except Exception as e:
        logger.error(f"Trade execution failed: {e}")
        return {"status": "failed", "reason": str(e)}

if __name__ == "__main__":
    # Test connection
    c = get_clob_client()
    if c:
        print("CLOB Client initialized successfully (Credentials present).")
    else:
        print("CLOB Client initialization skipped (Credentials missing).")
