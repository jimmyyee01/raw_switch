import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

# Carrier API credentials (environment variables)
CARRIER_A_KEY = os.getenv("CARRIER_A_KEY")
CARRIER_B_KEY = os.getenv("CARRIER_B_KEY")
CARRIER_C_KEY = os.getenv("CARRIER_C_KEY")

# Database settings
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sim_aggregator.db")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Switching thresholds
MIN_BANDWIDTH_MBPS = float(os.getenv("MIN_BANDWIDTH_MBPS", "10"))
MAX_LATENCY_MS = float(os.getenv("MAX_LATENCY_MS", "100"))
FAILURE_THRESHOLD = int(os.getenv("FAILURE_THRESHOLD", "3"))

# API key for auth (optional; set to empty string to disable)
API_KEY = os.getenv("API_KEY", "")


def get_carrier_key(carrier_name: str) -> str | None:
    """Look up a carrier API key by name.

    Searches environment variables first (CARRIER_X_KEY), then falls back to
    the global API_KEY if set.

    Returns:
        The API key string, or None if not found.
    """
    return globals().get(f"{carrier_name.upper()}_KEY") or API_KEY


def get_carrier_api_url(carrier_name: str) -> str | None:
    """Look up a carrier API base URL by name.

    Returns:
        The API base URL string, or None if not found.
    """
    env_var = f"{carrier_name.upper()}_API_URL"
    return os.getenv(env_var)
