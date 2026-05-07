import ee
import os
import uuid
from functools import wraps
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from fastapi import HTTPException

load_dotenv()

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
    "NDWI":      {"threshold":  0.1,  "operator": "gt", "palette": ["0000FF"]},  # blue  — water gain
    "NDVI":      {"threshold":  0.2,  "operator": "gt", "palette": ["00FF00"]},  # green — vegetation gain
    "NBR":       {"threshold": -0.1,  "operator": "lt", "palette": ["FF0000"]},  # red   — burn scars
    "LOG-RATIO": {"threshold": -20.0, "operator": "lt", "palette": ["0000FF"]},  # blue  — SAR water/flood
    "DEFAULT":   {"threshold":  0.0,  "operator": "gt", "palette": ["FF0000"]},
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
            scale=10,
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
            geom = ee.Geometry.Polygon(region['coordinates']) if region else image.geometry()
            stats = image.reduceRegion(
                reducer=ee.Reducer.mean().combine(ee.Reducer.stdDev(), sharedInputs=True),
                geometry=geom,
                scale=10,
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
        'format': 'png',
        'crs': 'EPSG:3857',
    }
    if region is not None:
        thumb_params['region'] = region

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
            low_cloud = filtered.filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 80))
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
def load_imagery_gee(aoi_dict: dict, date_range: list, sensor: str, locked_anchors: list = None):
    aoi = ee.Geometry.Polygon(aoi_dict["coordinates"])
    # Buffer the AOI by 2.5km to ensure we discover all potentially 'Beautiful' 
    # adjacent tiles. However, we will still prioritize the original AOI for anchoring.
    search_aoi = aoi.buffer(2500).bounds()
    
    start_date = date_range[0]
    end_date = date_range[1]

    if start_date == end_date:
        dt = datetime.strptime(end_date, "%Y-%m-%d")
        end_date = (dt + timedelta(days=1)).strftime("%Y-%m-%d")

    aoi_bounds = aoi.bounds().getInfo()["coordinates"]
    region_for_thumb = {"type": "Polygon", "coordinates": aoi_bounds}

    collection, used_sensor, high_cloud, window = _search_images_with_expansion(search_aoi, start_date, end_date, sensor)
    if collection is None and sensor == "Sentinel-2" and high_cloud:
        collection, used_sensor, _, window = _search_images_with_expansion(search_aoi, start_date, end_date, "Sentinel-1")

    if collection is None:
        return {"status": "no_data", "message": "No optical or SAR imagery available for the requested area and timeframe."}

    # Grab up to 200 images to ensure we discover all dates and tiles (prevents list truncation)
    image_list = collection.sort('system:time_start').toList(200)
    count = image_list.size().getInfo()
    if count == 0:
        return {"status": "no_data", "message": "No optical or SAR imagery available for the requested area and timeframe."}

    # --- Batch Metadata Fetch (Crucial to avoid 401 timeouts) ---
    # We fetch all indices and cloud cover metrics in ONE server-side call.
    # IMPORTANT: Sentinel-1 (SAR) does NOT have CLOUDY_PIXEL_PERCENTAGE.
    try:
        if used_sensor == "Sentinel-2":
            selectors = ['system:index', 'CLOUDY_PIXEL_PERCENTAGE']
            repeat_count = 2
        else:
            # For SAR, we use (relativeOrbitNumber_start + orbitProperties_pass) 
            # to group imagery by viewing geometry.
            selectors = ['system:index', 'relativeOrbitNumber_start', 'orbitProperties_pass']
            repeat_count = 3
            
        raw_metadata = collection.reduceColumns(
            reducer=ee.Reducer.toList().repeat(repeat_count),
            selectors=selectors
        ).getInfo().get('list', [[], [], []])
        
        # Guard against Earth Engine returning ['list': []] instead of [[], []]
        if not isinstance(raw_metadata, list) or len(raw_metadata) < 2:
            raw_metadata = [[], []]
    except Exception as e:
        logger.error(f"[gee_services] Metadata batch fetch failed for {used_sensor}: {e}")
        raw_metadata = [[], []]

    from collections import defaultdict
    grouped_by_date = defaultdict(list)   # stores (ee.Image, mgrs_code, cloud_cover) tuples
    
    system_indices = raw_metadata[0]
    anchor_values = raw_metadata[1] # For S2: CC %; For S1: Relative Orbit Number
    orbit_passes = raw_metadata[2] if len(raw_metadata) > 2 else [] # For S1: ASCENDING/DESCENDING

    for i in range(count):
        # We fetch the image and its basic properties
        image = ee.Image(image_list.get(i))
        
        # 1. MGRS/Orbit Code (The Spatial Anchor)
        sys_idx = system_indices[i] if i < len(system_indices) else ""
        if used_sensor == "Sentinel-2":
            mgrs = sys_idx.split('_')[-1] if '_' in sys_idx else ""
        else:
            # For SAR, use (Relative Orbit + Pass Direction) as the anchor
            orbit = str(anchor_values[i]) if i < len(anchor_values) else ""
            direction = str(orbit_passes[i]) if i < len(orbit_passes) else ""
            mgrs = f"{orbit}_{direction}"
        
        # 2. Cloud Cover / Quality Metric
        if used_sensor == "Sentinel-2":
            cc = float(anchor_values[i]) if (i < len(anchor_values) and anchor_values[i] is not None) else 100.0
        else:
            cc = 0.0 # SAR is cloud-penetrating
            
        # 3. Timestamp (Use pre-fetched list value if available, else getInfo fallback)
        try:
            # S2 index format: 20220826T055639_...
            # S1 index format: S1A_IW_GRDH_1SDV_20220412T...
            date_part = ""
            if sys_idx:
                parts = sys_idx.split('_')
                for p in parts:
                    if len(p) >= 8 and p[:8].isdigit() and 'T' in p:
                        date_part = p[:8]
                        break
            
            if date_part:
                date_str = datetime.strptime(date_part, '%Y%m%d').strftime('%Y-%m-%d')
            else:
                ts_ms = image.get('system:time_start').getInfo()
                date_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d') if ts_ms else "unknown"
        except:
            date_str = "unknown"

        # We store sys_idx for deterministic tie-breaking in tile selection
        grouped_by_date[date_str].append((image, mgrs, cc, sys_idx))

    # Keep only the first 4 dates
    sorted_dates = sorted([d for d in grouped_by_date.keys() if d != "unknown"])[:4]

    # ── Per-date tile selection: consistent MGRS tile across all dates ─────────
    # Problem: picking lowest-cloud tile independently per date causes different
    # MGRS tiles to win on different dates → alternating geographic extents.
    # Solution (pure selection, no geometry math):
    #   1. Pick the best (lowest-cloud) tiles for the FIRST date (up to 2).
    #   2. Record their MGRS codes (e.g. ["T42RVN", "T42RVP"]).
    #   3. For every subsequent date, prefer tiles with those exact MGRS codes.
    #   4. Fall back to lowest-cloud selection only if those anchors are unavailable.

    def _best_tiles(tile_list, count=2):
        """Top 'count' clearest UNIQUE tiles, with deterministic tie-breaking."""
        if not tile_list:
            return []
        
    def _best_tiles(tile_list, count=2):
        """Return the top unique tiles by cloud cover / stability."""
        if not tile_list:
            return []
        
        # Group by anchor (mgrs/orbit) and keep the clearest image for each
        best_per_anchor = {}
        for img, mgrs, cc, sys_idx in tile_list:
            # For SAR (cc=0), use sys_idx as a deterministic tie-breaker for slice selection
            if mgrs not in best_per_anchor or cc < best_per_anchor[mgrs][1] or (cc == best_per_anchor[mgrs][1] and sys_idx < best_per_anchor[mgrs][2]):
                best_per_anchor[mgrs] = (img, cc, sys_idx)
        
        # Sort unique anchors: primary by cloud cover, secondary by MGRS code for stability
        sorted_anchors = sorted(
            best_per_anchor.items(), 
            key=lambda x: (x[1][1], x[0]) 
        )
        return [val[0] for key, val in sorted_anchors[:count]]

    def _consistent_tiles(tile_list, preferred_anchors, count=2):
        """Return unique tiles matching preferred_anchors, filling up with best uniques if needed."""
        selected = []
        anchors_seen = set()
        
        # 1. Priority: Exact matches for preferred anchors (one image per anchor)
        if preferred_anchors:
            # Sort tile_list by CC (index 2) first, then sys_idx (index 3) for deterministic SAR slice selection
            sorted_candidates = sorted(tile_list, key=lambda t: (t[2], t[3]))
            
            for img, mgrs, cc, sys_idx in sorted_candidates:
                if mgrs in preferred_anchors and mgrs not in anchors_seen:
                    selected.append(img)
                    anchors_seen.add(mgrs)
        
        # 2. Fill: Best remaining unique tiles if we are below count
        if len(selected) < count:
            remaining = [t for t in tile_list if t[1] not in anchors_seen]
            best_uniques = _best_tiles(remaining, count - len(selected))
            for img in best_uniques:
                selected.append(img)
            
        return selected[:count]

    # Use locked_anchors if provided, else perform 'Master Anchor Bootstrapping' 
    # across the entire sequence to find the most stable dual-tile frame.
    preferred_anchors = locked_anchors
    if not preferred_anchors:
        # Identify anchors present on the FIRST date (crucial for initial framing)
        first_date_anchors = set(m for img, m, cc, _ in grouped_by_date[sorted_dates[0]])
        
        anchor_stats = {} # mgrs -> {on_first_date, total_count, best_cc}
        for d in sorted_dates:
            for img, mgrs, cc, _ in grouped_by_date[d]:
                if mgrs not in anchor_stats:
                    anchor_stats[mgrs] = {
                        "first": 1 if mgrs in first_date_anchors else 0,
                        "count": 0,
                        "best_cc": 100.0
                    }
                anchor_stats[mgrs]["count"] += 1
                if cc < anchor_stats[mgrs]["best_cc"]:
                    anchor_stats[mgrs]["best_cc"] = cc
        
        # Select Master Anchors by priority: Date 1 presence > Count > CC > ID
        sorted_masters = sorted(
            [m for m in anchor_stats.keys() if m], # Skip empty strings
            key=lambda m: (
                -anchor_stats[m]["first"], 
                -anchor_stats[m]["count"], 
                anchor_stats[m]["best_cc"],
                m
            )
        )
        preferred_anchors = sorted_masters[:2]

    selected_images_by_date = {}
    for d in sorted_dates:
        selected_images_by_date[d] = _consistent_tiles(grouped_by_date[d], preferred_anchors, count=2)

    collection_path = "COPERNICUS/S2_HARMONIZED" if used_sensor == "Sentinel-2" else "COPERNICUS/S1_GRD"
    images = []
    # Download thumbnails for selected tiles (bounded to 2 per date)
    for d in sorted_dates:
        for image in selected_images_by_date[d]:
            bare_id = image.id().getInfo()
            asset_id = f"{collection_path}/{bare_id}" if bare_id and not bare_id.startswith("COPERNICUS/") else bare_id

            roi = image.geometry().bounds()

            if used_sensor == "Sentinel-2":
                img_transparent = image.updateMask(image.select('B4').gt(0))
                thumbnail_url = img_transparent.getThumbURL({
                    'bands': ['B4', 'B3', 'B2'],
                    'min': 0,
                    'max': 3000,
                    'dimensions': 512,
                    'format': 'png',
                    'region': image.geometry(),
                    'crs': 'EPSG:3857',
                })
            else:
                # SAR Rendering: use the same 512px/EPSG:3857 logic as Optical
                # We remove .clip(aoi) here because the 'region' parameter handles the crop.
                thumbnail_url = _render_thumbnail(image, used_sensor, asset_id, region=image.geometry())

            _validate_thumb_url(thumbnail_url, asset_id)

            # CRITICAL: Capturing pixels immediately prevents 401 token expiry.
            import requests as _req, base64 as _b64, time as _time, random as _random
            try:
                # Robust retry loop for GEE thumbnail fetching
                max_retries = 8
                for attempt in range(max_retries):
                    try:
                        _r = _req.get(thumbnail_url, timeout=30)
                        _r.raise_for_status()
                        thumbnail_url = "data:image/png;base64," + _b64.b64encode(_r.content).decode("utf-8")
                        break # Success
                    except Exception as e:
                        if attempt == max_retries - 1: raise e
                        _time.sleep(1 + _random.uniform(0.5, 2.5)) # Randomized jittered delay
            except Exception:
                pass  # Fallback to raw URL if download fails after retries

            metadata = _extract_image_metadata(image, used_sensor, region=region_for_thumb)
            images.append({
                "asset_id": asset_id,
                "thumbnail_url": thumbnail_url,
                "metadata": metadata,
            })

    return {
        "status": "success", 
        "data": {
            "images": images, 
            "resolved_aoi": aoi_dict,
            "anchors_used": preferred_anchors
        }
    }


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
        if index_type == "Log-Ratio":
            # For SAR water detection, we use VV as a proxy for the 'Log' of backscatter.
            # (In S1_GRD, VV is already in dB, which is a logarithmic scale).
            index_img = vv.rename("Log-Ratio")
        else:
            # Default SAR: use VV directly
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
    **kwargs
):
    """
    Compute change-detection masks from a list of raw GEE asset IDs.
    asset_ids must be valid COPERNICUS/* or projects/* strings — never thumbnail URLs.
    """
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
                    scale=100,       # <--- CHANGED TO 100
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
            roi = img.geometry()
            mask_url = visual_mask.getThumbURL({
                "palette": config["palette"],
                "dimensions": 512,
                "format": "png",
                "region": roi,
                "crs": "EPSG:3857",
            })
            _validate_thumb_url(mask_url, f"mask for asset {asset_id}")

            # ROBUST BASE64 CAPTURE
            import requests as _req, base64 as _b64, sys as _sys, time as _time, random as _random
            try:
                # Robust retry loop for GEE thumbnail fetching
                max_retries = 8
                encoded_successfully = False
                for attempt in range(max_retries):
                    try:
                        _r = _req.get(mask_url, timeout=30)
                        if _r.status_code == 200:
                            mask_url = "data:image/png;base64," + _b64.b64encode(_r.content).decode("utf-8")
                            encoded_successfully = True
                            break
                        else:
                            if attempt == max_retries - 1:
                                print(f"BACKEND ERROR: GEE returned {_r.status_code} for mask URL after {max_retries} attempts", file=_sys.stderr)
                            _time.sleep(1 + _random.uniform(0.5, 2.5))
                    except Exception as _e:
                        if attempt == max_retries - 1:
                            print(f"BACKEND EXCEPTION: Failed to encode mask: {_e}", file=_sys.stderr)
                        _time.sleep(1 + _random.uniform(0.5, 2.5))
            except Exception:
                pass # Fallback to URL

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
                    scale=100,       # <--- CHANGED TO 100
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
            mask_map[asset_id] = None
            trend_analysis_parts.append(f"Mask generation failed for asset {asset_id}: {e}")

    trend_analysis = " ".join(trend_analysis_parts) if trend_analysis_parts else "No significant changes detected."
    return {"status": "success", "data": {"computed_masks": mask_map, "trend_analysis": trend_analysis}}