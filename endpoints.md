# AutoCritic-EO Backend API Contract

The determinist Tool Registry for AutoCritic-EO.

## Multi-Temporal Batch Processing Endpoints

### 1. Data Availability Fallback
**POST** `/api/check_availability`

**Request Body:**
```json
{
  "aoi": {
    "type": "Polygon",
    "coordinates": [[[-122.092, 37.424], [-122.086, 37.424], [-122.086, 37.418], [-122.092, 37.418], [-122.092, 37.424]]]
  },
  "date_range": ["2023-01-01", "2023-01-31"],
  "sensor": "Sentinel-2"
}
```

**Response (Success):**
```json
{
  "status": "success",
  "data": {
    "images_found": 3,
    "latest_pass_date": "2023-01-29"
  }
}
```

---

### 2. Batch Imagery Extractor
**POST** `/api/load_imagery`

**Request Body:** Same as `/api/check_availability`

**Response:**
```json
{
  "status": "success",
  "data": {
    "file_list": [
      "https://url_date1...",
      "https://url_date2...",
      "https://url_date3..."
    ]
  }
}
```

---

### 3. The Spatial Math Engine (Modular Index Toolkit)
**POST** `/api/compute_mask`

**Request Body:**
```json
{
  "file_list": ["url_date1", "url_date2", "url_date3"],
  "sensor": "Sentinel-2",
  "index_type": "NDWI"
}
```

**Response:**
```json
{
  "status": "success",
  "data": {
    "computed_masks": ["mask_date1", "mask_date2", "mask_date3"],
    "trend_analysis": "increasing"
  }
}
```

---

### 4. The Adversarial Mocking Endpoint
**POST** `/api/mock/adversarial_optical`

**Request Body:** Same as `/api/check_availability`

**Response:**
```json
{
  "status": "success",
  "data": {
    "file_list": ["CLOUD_OBSCURED.png", "NODATA_STRIPES.png", "NDVI_EXCEEDS_1.png"]
  }
}
```
