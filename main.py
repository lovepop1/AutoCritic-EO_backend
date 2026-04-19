import uuid
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from database import get_imagery_cache, set_imagery_cache, get_mask_cache, set_mask_cache
import gee_services
from datetime import datetime, timedelta
import re
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable

app = FastAPI(title="AutoCritic-EO Backend", description="GIS Backend API for AutoCritic-EO")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# AOI mapping for locations
AOI_MAP = {
    "Valencia": {"type": "Polygon", "coordinates": [[[-0.5, 39.3], [-0.2, 39.3], [-0.2, 39.6], [-0.5, 39.6], [-0.5, 39.3]]]},
    "Madrid": {"type": "Polygon", "coordinates": [[[-3.8, 40.3], [-3.5, 40.3], [-3.5, 40.6], [-3.8, 40.6], [-3.8, 40.3]]]},
    "Barcelona": {"type": "Polygon", "coordinates": [[[2.0, 41.3], [2.3, 41.3], [2.3, 41.6], [2.0, 41.6], [2.0, 41.3]]]},
}

SENSOR_MAP = {
    "optical": "Sentinel-2",
    "Sentinel-2": "Sentinel-2",
    "SAR": "Sentinel-1",
    "Sentinel-1": "Sentinel-1",
}

geolocator = Nominatim(user_agent="autocritic_eo_backend", timeout=10)

# Global state for vertex outputs
vertex_outputs = {}


def _is_valid_url_list(items: list) -> bool:
    """Return True only if every entry is a real https:// URL (not a stale asset ID)."""
    return (
        isinstance(items, list)
        and len(items) > 0
        and all(
            isinstance(u, str) and u.startswith("https://")
            if isinstance(u, str)
            else isinstance(u, dict) and isinstance(u.get("image_url"), str) and u["image_url"].startswith("https://")
            for u in items
        )
    )

def geocode_location_to_aoi(location: str):
    location = location.strip()
    if not location:
        return None
    if location in AOI_MAP:
        return AOI_MAP[location]
    try:
        geo = geolocator.geocode(location, exactly_one=True)
    except (GeocoderTimedOut, GeocoderUnavailable):
        return None
    if not geo or not getattr(geo, "raw", None):
        return None
    bbox = geo.raw.get("boundingbox")
    if not bbox or len(bbox) != 4:
        return None
    south, north, west, east = map(float, bbox)
    
    # Constrain bounding box to max ~25-30km span (0.25 deg) to prevent massive GEE payloads
    height, width = north - south, east - west
    max_span = 0.25
    if height > max_span or width > max_span:
        lat_center, lon_center = (north + south) / 2.0, (east + west) / 2.0
        height, width = min(height, max_span), min(width, max_span)
        south, north = lat_center - (height / 2.0), lat_center + (height / 2.0)
        west, east = lon_center - (width / 2.0), lon_center + (width / 2.0)
        
    return {
        "type": "Polygon",
        "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]]
    }


def resolve_aoi(request: dict):
    if isinstance(request.get("aoi"), dict):
        return request["aoi"]
    location = request.get("location")
    if isinstance(location, str) and location.strip():
        aoi = geocode_location_to_aoi(location)
        if aoi:
            return aoi
    if isinstance(location, dict) and location.get("type") == "Polygon":
        return location
    raise ValueError("No valid AOI or location provided")

def parse_date_range(date_param):
    """Convert AI date parameter to GEE date range."""
    if isinstance(date_param, str):
        normalized = date_param.strip()
        if " to " in normalized:
            parts = [part.strip() for part in normalized.split(" to ", 1)]
            if len(parts) == 2 and parts[0] and parts[1]:
                return parts
        if re.match(r"^\d{4}-\d{2}-\d{2}$", normalized):
            date_obj = datetime.fromisoformat(normalized)
            start_date = (date_obj - timedelta(days=1)).strftime('%Y-%m-%d')
            end_date = (date_obj + timedelta(days=1)).strftime('%Y-%m-%d')
            return [start_date, end_date]
        if re.match(r"^[A-Za-z]+ \d{4}$", normalized):
            try:
                month_date = datetime.strptime(normalized, "%B %Y")
                start_date = month_date.strftime('%Y-%m-%d')
                if month_date.month == 12:
                    end_date = datetime(month_date.year + 1, 1, 1) - timedelta(days=1)
                else:
                    end_date = datetime(month_date.year, month_date.month + 1, 1) - timedelta(days=1)
                return [start_date, end_date.strftime('%Y-%m-%d')]
            except ValueError:
                pass
        try:
            date_obj = datetime.fromisoformat(normalized)
            start_date = (date_obj - timedelta(days=1)).strftime('%Y-%m-%d')
            end_date = (date_obj + timedelta(days=1)).strftime('%Y-%m-%d')
            return [start_date, end_date]
        except ValueError:
            return ["2024-10-29", "2024-10-31"]
    elif isinstance(date_param, list) and len(date_param) == 2:
        return date_param
    else:
        return ["2024-10-29", "2024-10-31"]

def resolve_file_reference(file_param):
    """Resolve vertex references like 'T1' to actual file paths."""
    if isinstance(file_param, str):
        # Check if it's a vertex reference (T1, T2, etc.)
        if file_param.startswith('T') and file_param[1:].isdigit():
            vertex_id = file_param
            if vertex_id in vertex_outputs:
                output = vertex_outputs[vertex_id]
                if 'image_ids' in output:
                    return output['image_ids']
                elif 'computed_masks' in output:
                    return output['computed_masks']
            return []  # Empty if vertex not executed yet
        else:
            # Single file path
            return [file_param]
    elif isinstance(file_param, list):
        # Resolve recursively for multi-temporal sequences
        resolved = []
        for item in file_param:
            res = resolve_file_reference(item)
            if isinstance(res, list):
                resolved.extend(res)
            else:
                resolved.append(res)
        return resolved
    else:
        return []

@app.post("/api/v1/check_availability")
async def check_availability(request: dict):
    try:
        aoi = resolve_aoi(request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    date_param = request.get("date_range") or request.get("date") or "2024-10-30"
    sensor = request.get("sensor", "optical")
    date_range = parse_date_range(date_param)
    sensor = SENSOR_MAP.get(sensor, "Sentinel-2")

    result = gee_services.check_availability_gee(aoi, date_range, sensor)
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result["message"])
    if result.get("status") == "no_data":
        location = request.get("location", "requested location")
        return {
            "status": "no_data",
            "message": f"No optical or SAR imagery available for {location} within the expanded +/- 14 day timeframe."
        }

    return {
        "status": "success",
        "data": {
            "images_found": result["data"]["images_found"]
        }
    }

@app.post("/api/v1/load_imagery")
async def load_imagery(request: dict):
    print("LOAD IMAGERY PAYLOAD:", request)
    adversarial = bool(request.get("adversarial", False))

    try:
        aoi = resolve_aoi(request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid payload received: {request}. Error: {str(e)}")

    date_range_param = request.get("date_range") or request.get("date") or ["2024-10-29", "2024-10-31"]
    sensor = request.get("sensor", "optical")
    date_range = parse_date_range(date_range_param)
    sensor = SENSOR_MAP.get(sensor, "Sentinel-2")

    cache_key = (str(aoi), str(date_range), sensor)
    cached_files = get_imagery_cache(*cache_key)
    if not adversarial and cached_files is not None and _is_valid_url_list(cached_files):
        return {"status": "success", "data": {"images": cached_files}}

    result = gee_services.load_imagery_gee(aoi, date_range, sensor)
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result["message"])
    if result.get("status") == "no_data":
        location = request.get("location", "requested location")
        return {
            "status": "no_data",
            "message": f"No optical or SAR imagery available for {location} within the expanded +/- 14 day timeframe."
        }

    images = result["data"]["images"]

    # --- Live adversarial corruption: mutate real GEE metadata before returning ---
    if adversarial:
        for img in images:
            # Trap 1: claim visually clear image is fully cloud-obscured
            img["metadata"]["cloud_cover_percent"] = 99.9
            # Trap 2: force a CRS mismatch on a correctly projected image
            img["metadata"]["crs_info"]["alignment"] = "MISMATCH"
            img["metadata"]["crs_info"]["actual"] = "EPSG:32630"
            img["metadata"]["adversarial_corrupted"] = True

    if not adversarial:
        set_imagery_cache(*cache_key, images)

    vertex_id = request.get("vertex_id")
    if vertex_id:
        # Store asset IDs for stateless downstream routing
        vertex_outputs[vertex_id] = {"image_ids": [img["asset_id"] for img in images]}

    response_data = {"status": "success", "data": {"images": images}}
    print(f"LOAD IMAGERY RESPONSE: {response_data}")
    return response_data

@app.post("/api/v1/compute_mask")
async def compute_mask(request: dict):
    print("RECEIVED PAYLOAD:", request)
    image_ids_param = request.get("image_ids", request.get("file_list", []))
    asset_ids = resolve_file_reference(image_ids_param)

    # Reject thumbnail URLs — the orchestrator must pass asset_id strings
    bad = [v for v in asset_ids if isinstance(v, str) and v.startswith("https://")]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=(
                "compute_mask received thumbnail URLs instead of asset IDs. "
                "Extract the asset_id field from load_imagery images and pass those."
            ),
        )

    if not asset_ids:
        raise HTTPException(status_code=400, detail=f"Missing required asset IDs. Invalid image_ids received: {image_ids_param}")

    index = request.get("index") or request.get("index_type") or "NBR"
    threshold = request.get("threshold", -0.2)
    sensor = SENSOR_MAP.get(request.get("sensor", "Sentinel-2"), "Sentinel-2")
    adversarial = bool(request.get("adversarial", False))

    cache_key = (str(asset_ids), index)
    cached_mask = get_mask_cache(*cache_key)
    if not adversarial and cached_mask is not None and _is_valid_url_list(cached_mask.get("computed_masks", [])):
        result = {"status": "success", "data": cached_mask}
    else:
        result = gee_services.compute_mask_gee(asset_ids, sensor, index, threshold, adversarial=adversarial)
        if result.get("status") == "error":
            raise HTTPException(status_code=500, detail=result["message"])

        data = result["data"]
        if not adversarial:
            set_mask_cache(*cache_key, data["computed_masks"], data["trend_analysis"])

    vertex_id = request.get("vertex_id")
    if vertex_id:
        vertex_outputs[vertex_id] = result["data"]

    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
