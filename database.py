import hashlib
import json
from supabase import create_client
import os
from dotenv import load_dotenv

load_dotenv()

supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")

if not supabase_url or not supabase_key:
    raise ValueError("WARNING: Supabase URL and Key must be defined in .env")

supabase = create_client(supabase_url, supabase_key)

def generate_hash(data: dict) -> str:
    """Generate SHA-256 hash for reproducible queries."""
    data_str = json.dumps(data, sort_keys=True)
    return hashlib.sha256(data_str.encode("utf-8")).hexdigest()

def get_imagery_cache(aoi: dict, date_range: list, sensor: str, locked_tiles: list = None):
    query_hash = generate_hash({
        "aoi": aoi,
        "date_range": date_range,
        "sensor": sensor,
        "locked_tiles": locked_tiles or [],
    })
    res = supabase.table("imagery_cache").select("*").eq("query_hash", query_hash).execute()
    if res.data:
        return res.data[0].get("file_list", [])
    return None

def set_imagery_cache(aoi: dict, date_range: list, sensor: str, file_list: list, locked_tiles: list = None):
    query_hash = generate_hash({
        "aoi": aoi,
        "date_range": date_range,
        "sensor": sensor,
        "locked_tiles": locked_tiles or [],
    })
    data = {
        "query_hash": query_hash,
        "sensor": sensor,
        "date_range": date_range,
        "file_list": file_list
    }
    supabase.table("imagery_cache").upsert(data).execute()

def get_mask_cache(file_list: list, index_type: str):
    query_hash = generate_hash({
        "file_list": file_list,
        "index_type": index_type
    })
    res = supabase.table("mask_cache").select("*").eq("query_hash", query_hash).execute()
    if res.data:
        return {
            "computed_masks": res.data[0].get("computed_masks", []),
            "trend_analysis": res.data[0].get("trend_analysis", "")
        }
    return None

def set_mask_cache(file_list: list, index_type: str, computed_masks: list, trend_analysis: str):
    query_hash = generate_hash({
        "file_list": file_list,
        "index_type": index_type
    })
    data = {
        "query_hash": query_hash,
        "index_type": index_type,
        "computed_masks": computed_masks,
        "trend_analysis": trend_analysis
    }
    supabase.table("mask_cache").upsert(data).execute()