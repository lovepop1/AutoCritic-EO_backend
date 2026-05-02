import ee
import os
import logging
import re
import time
import uuid
from functools import wraps
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from fastapi import HTTPException

logger = logging.getLogger(__name__)

SERVICE_ACCOUNT = os.environ["GEE_SERVICE_ACCOUNT"]
KEY_FILE = os.environ["GEE_KEY_FILE"]

try:
    credentials = ee.ServiceAccountCredentials(SERVICE_ACCOUNT, KEY_FILE)
    ee.Initialize(credentials)
    print("GEE Initialized Successfully!")
except Exception as e:
    print(f"Failed to initialize GEE: {str(e)}")

def catch_ee_errors(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ee.EEException as e:
            return {"status": "error", "message": f"GEE Exception: {str(e)}"}
        except Exception as e:
            return {"status": "error", "message": f"Server Error: {str(e)}"}
    return wrapper


def _expand_date_window(start_date: str, end_date: str, days: int):
    start = datetime.fromisoformat(start_date) - timedelta(days=days)
    end = datetime.fromisoformat(end_date) + timedelta(days=days)
    return start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')


def _get_collection(sensor: str):
    if sensor == "Sentinel-2":
        return ee.ImageCollection("COPERNICUS/S2_HARMONIZED")
    return ee.ImageCollection("COPERNICUS/S1_GRD")


thumbnail_to_asset_map = {}  # retained for legacy callers; no longer written to by load_imagery_gee

# Per-index visualisation config: threshold, logical operator, and colour palette.
# The threshold here is applied to the *difference* image (idx2 - idx1).
INDEX_VIS_CONFIG = {
    "NDWI":    {"threshold":  0.1,  "operator": "gt", "palette": ["0000FF"]},  # blue  — water gain
    "NDVI":    {"threshold":  0.2,  "operator": "gt", "palette": ["00FF00"]},  # green — vegetation gain
    "NBR":     {"threshold": -0.1,  "operator": "lt", "palette": ["FF0000"]},  # red   — burn scars
    "DEFAULT": {"threshold":  0.0,  "operator": "gt", "palette": ["FF0000"]},
}


def _sanitized_filename(ext: str = "png") -> str:
    """Return a generic UUID-based filename with no semantic content."""
    return f"tmp_{uuid.uuid4().hex[:8]}.{ext}"


def _extract_image_metadata(image, sensor: str, region=None) -> dict:
    """
    Compute strictly typed metadata for a single GEE image.
    Returns a dict matching the ImageryMetadata schema.
    """
    # --- Timestamp ---
    ts_ms = image.get("system:time_start").getInfo()
    if ts_ms:
        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
    else:
        ts = datetime.now(tz=timezone.utc).isoformat()

    # --- Cloud cover ---
    if sensor == "Sentinel-2":
        cloud_cover = image.get("CLOUDY_PIXEL_PERCENTAGE").getInfo() or 0.0
    else:
        cloud_cover = 0.0  # SAR is not affected by cloud cover

    # --- CRS ---
    try:
        proj = image.select(0).projection().getInfo()
        actual_crs = proj.get("crs", "UNKNOWN")
    except Exception:
        actual_crs = "UNKNOWN"
    alignment = (
        "MATCH"
        if (
            actual_crs == "EPSG:4326"
            or actual_crs.startswith("EPSG:326")   # UTM North zones
            or actual_crs.startswith("EPSG:327")   # UTM South zones
        )
        else "MISMATCH"
    )

    # --- Index stats (band 0 min/max over region) ---
    try:
        geom = ee.Geometry.Polygon(region["coordinates"]) if region else image.geometry()
        stats = image.select(0).reduceRegion(
            reducer=ee.Reducer.minMax(),
            geometry=geom,
            scale=100,
            maxPixels=1e9,
        ).getInfo()
        band_name = list(stats.keys())[0].rsplit("_", 1)[0] if stats else "b1"
        idx_min = float(stats.get(f"{band_name}_min") or 0.0)
        idx_max = float(stats.get(f"{band_name}_max") or 1.0)
    except Exception:
        idx_min, idx_max = 0.0, 1.0

    return {
        "cloud_cover_percent": float(cloud_cover),
        "crs_info": {
            "expected": "EPSG:4326 | UTM",
            "actual": actual_crs,
            "alignment": alignment,
        },
        "optical_reflectance": {"min": idx_min, "max": idx_max},
        "timestamp": ts,
    }


def _validate_thumb_url(url: str, context: str = "") -> str:
    """Ensure GEE returned a real https:// URL and not an asset ID or garbage."""
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError(
            f"GEE did not return a valid thumbnail URL{' for ' + context if context else ''}. "
            f"Got: {url!r}. Ensure the image has a valid region and vis params."
        )
    return url


def _render_thumbnail(image, sensor: str, asset_id: str = None, region=None):
    if sensor == "Sentinel-2":
        vis_params = {'bands': ['B4', 'B3', 'B2'], 'min': 0, 'max': 3000}
    else:
        # Calculate 1-standard-deviation stretch for Sentinel-1
        try:
            # region may be an ee.Geometry or a GeoJSON dict
            if isinstance(region, dict):
                geom = ee.Geometry.Polygon(region['coordinates'])
            elif region is not None:
                geom = region  # already an ee.Geometry
            else:
                geom = image.geometry()
            stats = image.reduceRegion(
                reducer=ee.Reducer.mean().combine(ee.Reducer.stdDev(), sharedInputs=True),
                geometry=geom,
                scale=100,
                maxPixels=1e9
            ).getInfo()
            if stats and 'VV_mean' in stats and 'VV_stdDev' in stats and stats['VV_stdDev'] is not None:
                mean = stats['VV_mean']
                std = stats['VV_stdDev']
                vis_params = {'bands': ['VV'], 'min': mean - std, 'max': mean + std}
            else:
                vis_params = {'bands': ['VV'], 'min': -25, 'max': 0}
        except Exception:
            vis_params = {'bands': ['VV'], 'min': -25, 'max': 0}

    thumb_params = {
        'dimensions': 512,
        'format':     'png',
        'crs':        'EPSG:3857',
    }
    if region is not None:
        thumb_params['region'] = region  # ee.Geometry — same raw tile polygon as S2 and mask

    url = image.visualize(**vis_params).getThumbURL(thumb_params)
    _validate_thumb_url(url, asset_id or "unknown image")
    return url


def _is_asset_id(value: str):
    # Covers COPERNICUS/S2_*, COPERNICUS/S1_GRD/*, and user/project assets
    return isinstance(value, str) and (
        value.startswith("COPERNICUS/")
        or value.startswith("projects/")
        or value.startswith("users/")
    )


def _search_images_with_expansion(aoi, start_date: str, end_date: str, sensor: str):
    collection = _get_collection(sensor)
    high_cloud_found = False
    for delta in [0, 7, 14]:
        window_start, window_end = _expand_date_window(start_date, end_date, delta)
        filtered = collection.filterBounds(aoi).filterDate(window_start, window_end)
        if sensor == "Sentinel-2":
            low_cloud = filtered.filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 100))  # allow smoke/haze through
            low_count = low_cloud.size().getInfo()
            if low_count > 0:
                return low_cloud, sensor, False, (window_start, window_end)
            if filtered.size().getInfo() > 0:
                high_cloud_found = True
        else:
            count = filtered.size().getInfo()
            if count > 0:
                return filtered, sensor, False, (window_start, window_end)
    return None, sensor, high_cloud_found, None


@catch_ee_errors
def check_availability_gee(aoi_dict: dict, date_range: list, sensor: str):
    aoi = ee.Geometry.Polygon(aoi_dict["coordinates"])
    
    start_date = date_range[0]
    end_date = date_range[1]

    if start_date == end_date:
        dt = datetime.strptime(end_date, "%Y-%m-%d")
        end_date = (dt + timedelta(days=1)).strftime("%Y-%m-%d")

    collection, used_sensor, high_cloud, window = _search_images_with_expansion(aoi, start_date, end_date, sensor)
    if collection is None and sensor == "Sentinel-2" and high_cloud:
        collection, used_sensor, _, window = _search_images_with_expansion(aoi, start_date, end_date, "Sentinel-1")

    if collection is None:
        return {"status": "no_data", "message": "No optical or SAR imagery available for the requested area and timeframe."}

    count = collection.size().getInfo()
    if count == 0:
        return {"status": "no_data", "message": "No optical or SAR imagery available for the requested area and timeframe."}

    latest_image = ee.Image(collection.sort('system:time_start', False).first())
    latest_date = ee.Date(latest_image.get('system:time_start')).format('YYYY-MM-dd').getInfo()

    return {"status": "success", "data": {"images_found": count, "latest_pass_date": latest_date}}


@catch_ee_errors
def load_imagery_gee(aoi_dict: dict, date_range: list, sensor: str):
    aoi = ee.Geometry.Polygon(aoi_dict["coordinates"])

    start_date = date_range[0]
    end_date = date_range[1]

    if start_date == end_date:
        dt = datetime.strptime(end_date, "%Y-%m-%d")
        end_date = (dt + timedelta(days=1)).strftime("%Y-%m-%d")

    # Get the AOI bounding box as a GeoJSON-compatible region dict for getThumbURL
    aoi_bounds = aoi.bounds().getInfo()["coordinates"]
    region_for_thumb = {"type": "Polygon", "coordinates": aoi_bounds}

    collection, used_sensor, high_cloud, window = _search_images_with_expansion(aoi, start_date, end_date, sensor)
    if collection is None and sensor == "Sentinel-2" and high_cloud:
        collection, used_sensor, _, window = _search_images_with_expansion(aoi, start_date, end_date, "Sentinel-1")

    if collection is None:
        return {"status": "no_data", "message": "No optical or SAR imagery available for the requested area and timeframe."}

    # Increased from 10 to 60 to ensure we pull enough tiles to span multiple dates
    image_list = collection.sort('system:time_start').toList(60)
    count = image_list.size().getInfo()

    if count == 0:
        return {"status": "no_data", "message": "No optical or SAR imagery available for the requested area and timeframe."}

    # Collection path prefix needed to build a fully-qualified asset ID
    collection_path = "COPERNICUS/S2_HARMONIZED" if used_sensor == "Sentinel-2" else "COPERNICUS/S1_GRD"

    images = []
    for i in range(count):
        image = ee.Image(image_list.get(i))
        bare_id = image.id().getInfo()
        # ee.Image(bare_id) would fail — GEE requires the full collection-qualified path
        asset_id = f"{collection_path}/{bare_id}" if bare_id and not bare_id.startswith("COPERNICUS/") else bare_id

        # --- Optical thumbnail: identical region + CRS to mask thumbnails ---
        roi = image.geometry().bounds()  # north-up axis-aligned bounding box
        if used_sensor == "Sentinel-2":
            # Mask out NoData (black fill) pixels so background is transparent
            img_transparent = image.updateMask(image.select('B4').gt(0))
            thumbnail_url = img_transparent.getThumbURL({
                'bands': ['B4', 'B3', 'B2'],
                'min': 0,
                'max': 3000,
                'dimensions': 512,
                'format': 'png',
                'region': image.geometry(),  # raw tile geometry — no .bounds() squish
                'crs': 'EPSG:3857',          # Web Mercator — prevents rotation, matches mask
            })
        else:
            # Sentinel-1: compute dynamic stretch then render
            thumbnail_url = _render_thumbnail(image.clip(aoi), used_sensor, asset_id, region=roi)

        _validate_thumb_url(thumbnail_url, asset_id)
        metadata = _extract_image_metadata(image, used_sensor, region=region_for_thumb)
        images.append({
            "asset_id": asset_id,
            "thumbnail_url": thumbnail_url,
            "metadata": metadata,
        })

    # Group the fetched images by date and return exactly 4 dates
    from collections import defaultdict
    grouped_by_date = defaultdict(list)
    for img_obj in images:
        date_str = img_obj["metadata"]["timestamp"][:10]
        grouped_by_date[date_str].append(img_obj)

    sorted_dates = sorted(list(grouped_by_date.keys()))[:4]
    final_images = []
    for d in sorted_dates:
        final_images.extend(grouped_by_date[d])

    return {"status": "success", "data": {"images": final_images, "resolved_aoi": aoi_dict}}

def _compute_index(img: ee.Image, sensor: str, index_type: str):
    """
    Compute a normalised spectral index for a single image.
    Routes to optical (S2) or SAR (S1) band logic based on sensor.
    Returns an ee.Image with a single band named after the index.
    """
    is_sar = sensor == "Sentinel-1"

    if is_sar:
        # SAR change proxy: log-ratio of VV backscatter (dB scale)
        vv = img.select("VV")
        vh = img.select("VH") if index_type == "Log-Ratio" else None
        if index_type == "Log-Ratio" and vh is not None:
            index_img = vv.subtract(vh).rename("Log-Ratio")
        else:
            # Default SAR: use VV directly as a single-band proxy
            index_img = vv.rename(index_type)
    else:
        # Optical (Sentinel-2) band routing
        if index_type == "NBR":
            nir = img.select("B8")
            swir = img.select("B12")
            index_img = nir.subtract(swir).divide(nir.add(swir)).rename("NBR")
        elif index_type == "NDVI":
            nir = img.select("B8")
            red = img.select("B4")
            index_img = nir.subtract(red).divide(nir.add(red)).rename("NDVI")
        elif index_type == "NDWI":
            green = img.select("B3")
            nir = img.select("B8")
            index_img = green.subtract(nir).divide(green.add(nir)).rename("NDWI")
        else:
            index_img = ee.Image.constant(0).rename(index_type)

    return index_img


@catch_ee_errors
def compute_mask_gee(
    asset_ids: list,
    sensor: str,
    index_type: str,
    threshold: float = -0.1,
    adversarial: bool = False,
    aoi_dict: dict = None,
):
    """
    Compute change-detection masks from a list of raw GEE asset IDs.
    asset_ids must be valid COPERNICUS/* or projects/* strings — never thumbnail URLs or date strings.
    """
    time.sleep(2.0)  # pace GEE requests to avoid throttling / SSLEOFError
    if not asset_ids or len(asset_ids) < 1:
        return {"status": "error", "message": "At least 1 asset ID required for mask generation"}

    # Validate every entry is a real asset ID — reject URLs/placeholders early as a 400
    invalid = [v for v in asset_ids if not _is_asset_id(v)]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=(
                "compute_mask received invalid IDs or placeholders. "
                "You MUST pass the actual 'asset_id' strings returned from load_imagery."
            ),
        )

    is_sar = sensor == "Sentinel-1"
    aoi = ee.Geometry.Polygon(aoi_dict["coordinates"]) if aoi_dict else None
    images = [ee.Image(aid) for aid in asset_ids]
    mask_map = {}          # asset_id → {thumbnail_url, metadata} | None  (1:1 with inputs)
    trend_analysis_parts = []

    for i, img in enumerate(images):
        asset_id = asset_ids[i]

        try:
            # --- Absolute spectral index for this single image ---
            index_img = _compute_index(img, sensor, index_type)

            # --- Adversarial flag: scale the absolute index to force impossible values ---
            anomaly_type = None
            if adversarial:
                index_img = index_img.multiply(1.5)
                anomaly_type = "INDEX_SCALING_ERROR"

            # --- Index config: threshold applied to the ABSOLUTE index, not a difference ---
            config = INDEX_VIS_CONFIG.get(
                index_type.upper() if index_type else "DEFAULT",
                INDEX_VIS_CONFIG["DEFAULT"],
            )

            if config["operator"] == "gt":
                binary_image = index_img.gt(config["threshold"])
            else:
                binary_image = index_img.lt(config["threshold"])

            visual_mask = binary_image.selfMask()

            # --- Server-side min/max reducer on the absolute index (for metadata) ---
            img_region = img.geometry().bounds().getInfo()["coordinates"]
            geom = ee.Geometry.Polygon(img_region)
            band_name = index_img.bandNames().get(0).getInfo()
            try:
                stats = index_img.reduceRegion(
                    reducer=ee.Reducer.minMax(),
                    geometry=geom,
                    scale=30,
                    maxPixels=1e9,
                ).getInfo()
                idx_min = float(stats.get(f"{band_name}_min") or 0.0)
                idx_max = float(stats.get(f"{band_name}_max") or 1.0)
            except Exception:
                idx_min, idx_max = 0.0, 1.0

            # --- CRS extraction ---
            try:
                actual_crs = img.select(0).projection().getInfo().get("crs", "UNKNOWN")
            except Exception:
                actual_crs = "UNKNOWN"
            crs_alignment = (
                "MATCH"
                if (
                    actual_crs == "EPSG:4326"
                    or actual_crs.startswith("EPSG:326")   # UTM North zones
                    or actual_crs.startswith("EPSG:327")   # UTM South zones
                )
                else "MISMATCH"
            )

            # --- Thumbnail ---
            # Force the mask thumbnail to perfectly overlay the optical thumbnails by using
            # the same AOI bounding box region that load_imagery_gee passes to _render_thumbnail.
            if aoi is not None:
                thumb_region = {"type": "Polygon", "coordinates": aoi.bounds().getInfo()["coordinates"]}
            else:
                thumb_region = geom  # fallback to tile geometry if no AOI provided
            mask_url = visual_mask.getThumbURL({
                "palette": config["palette"],
                "dimensions": 512,
                "format": "png",
                "region": thumb_region,
                "crs": "EPSG:3857",
            })
            _validate_thumb_url(mask_url, f"mask for asset {asset_id}")

            # --- Cloud cover (optical only) ---
            if not is_sar:
                try:
                    cloud_cover = float(img.get("CLOUDY_PIXEL_PERCENTAGE").getInfo() or 0.0)
                except Exception:
                    cloud_cover = 0.0
            else:
                cloud_cover = 0.0

            # --- Timestamp ---
            try:
                ts_ms = img.get("system:time_start").getInfo()
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat() if ts_ms else datetime.now(tz=timezone.utc).isoformat()
            except Exception:
                ts = datetime.now(tz=timezone.utc).isoformat()

            mask_map[asset_id] = {
                "thumbnail_url": mask_url,
                "metadata": {
                    "cloud_cover_percent": cloud_cover,
                    "crs_info": {
                        "expected": "EPSG:4326 | UTM",
                        "actual": actual_crs,
                        "alignment": crs_alignment,
                    },
                    "index_stats": {"min": idx_min, "max": idx_max},
                    "timestamp": ts,
                    "anomaly_type": anomaly_type,
                },
            }

            # --- Trend analysis: absolute area where index threshold is met ---
            try:
                area_image = ee.Image.pixelArea().updateMask(binary_image)
                area_stats = area_image.reduceRegion(
                    reducer=ee.Reducer.sum(),
                    geometry=geom,
                    scale=10,       # native Sentinel-2 resolution
                    maxPixels=1e9,
                ).getInfo()
                area_sq_meters = list(area_stats.values())[0] if area_stats and list(area_stats.values()) else 0.0
                area_sq_km = area_sq_meters / 1e6
                trend_analysis_parts.append(
                    f"Asset {asset_id} computed {index_type} area: {area_sq_km:.2f} sq km."
                )
            except Exception:
                trend_analysis_parts.append(f"Asset {asset_id}: area calculation failed.")

        except Exception as e:
            # Tile failed entirely — record None so downstream indexing stays aligned
            print(f"CRITICAL GEE ERROR for {asset_id}: {str(e)}")
            mask_map[asset_id] = None
            trend_analysis_parts.append(f"Mask generation failed for asset {asset_id}: {e}")

    trend_analysis = " ".join(trend_analysis_parts) if trend_analysis_parts else "No significant changes detected."
    return {"status": "success", "data": {"computed_masks": mask_map, "trend_analysis": trend_analysis}}