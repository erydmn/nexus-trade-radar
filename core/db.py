from functools import lru_cache
from supabase import create_client, Client
from core.config import settings

@lru_cache(maxsize=1)
def get_supabase_client() -> Client:
    """
    Returns a unified, single instance of the Supabase client.
    Uses the URL and Key defined in the core.config Settings.
    """
    if not settings.supabase_url or not settings.supabase_key:
        raise ValueError("Supabase URL and Key must be set in the configuration.")
    return create_client(settings.supabase_url, settings.supabase_key.get_secret_value())
