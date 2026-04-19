# AutoCritic-EO GIS Backend API Contract
## For Backend Engineer Confirmation

Port: `8000`

The AI Orchestrator (Port 8001) will invoke these four endpoints. Please confirm that the GIS Backend implements:

---

### 1. POST `/api/v1/check_availability`

**Request:**
```json
{
  "date": "2024-10-30",
  "location": "Valencia",
  "sensor": "optical"
}
```

**Response (success):**
```json
{
  "status": "success",
  "data": {
    "images_found": 2
  }
}
```

**Notes:**
- Accepts any dict-based kwargs (location, region, aoi, disaster_type, sensor, date, etc.)
- Should return HTTP status 200 with the JSON above
- If failure, return non-200 status or `"status": "error"`

---

### 2. POST `/api/v1/load_imagery`

**Request:**
```json
{
  "date_range": "2024-10-30",
  "location": "Valencia",
  "sensor": "optical",
  "bands": "RGB"
}
```

**Response (success):**
```json
{
  "status": "success",
  "data": {
    "file_list": [
      "sequence_1.png",
      "sequence_2.png",
      "sequence_3.png"
    ]
  }
}
```

**Notes:**
- Multi-temporal array: `file_list` contains chronological image file paths
- Return the list as multi-temporal sequence (earliest to latest)
- If sensor is "SAR", return SAR format files
- Accepts any dict-based kwargs (date_range, location, sensor, region, bands, disaster_id, etc.)

---

### 3. POST `/api/v1/compute_mask`

**Request:**
```json
{
  "file_list": [
    "sequence_1.png",
    "sequence_2.png",
    "sequence_3.png"
  ],
  "index": "NBR",
  "threshold": -0.2
}
```

**Response (success):**
```json
{
  "status": "success",
  "data": {
    "computed_masks": [
      "mask_1.png",
      "mask_2.png"
    ],
    "trend_analysis": "Disaster logic expands chronologically over 2 intervals."
  }
}
```

**Notes:**
- `file_list`: array of input image paths
- `computed_masks`: output array of change-detection mask paths
- `index`: supports "NBR", "NDVI", "NDBI", etc.
- `threshold`: numeric threshold for change detection
- All multi-temporal arrays must maintain chronological order

---

### 4. POST `/api/v1/load_imagery` (with `adversarial=true`)

**Request:**
```json
{
  "date_range": "2024-10-30",
  "location": "Valencia",
  "adversarial": true
}
```

**Response (intentionally adversarial - should trigger critic failures):**
```json
{
  "status": "success",
  "data": {
    "file_list": [
      "mock_base.png",
      "CLOUD_OBSCURED.png",
      "NODATA_STRIPES.png",
      "NDVI_EXCEEDS_1.png"
    ]
  }
}
```

**Notes:**
- When `adversarial=true`, return images containing semantic anomalies
- Used for testing the Reflexion recovery loop
- File names act as markers for the VLM critic to detect failures

---

## Expected Error Handling

**If backend returns non-200 or non-success status:**
```json
{
  "status": "error",
  "message": "API Request Failed: <error details>"
}
```

The orchestrator's Reflexion loop will detect this and attempt recovery (up to 3 times by default).

---

## Testing Checklist

- [ ] All four endpoints are reachable at `http://localhost:8000`
- [ ] Each endpoint accepts arbitrary dict kwargs and responds with the schema above
- [ ] `file_list` and `computed_masks` maintain chronological order
- [ ] Status 200 is returned for success cases
- [ ] Non-200 or `"status": "error"` triggers orchestrator error handling
- [ ] Adversarial endpoint returns files with semantic anomaly markers (CLOUD_OBSCURED, NODATA_STRIPES, NDVI_EXCEEDS_1)

**Once implemented, reply with:**
> "GIS Backend ready on Port 8000 with all four endpoints implemented."

Then the orchestrator will switch from mock mode to live API calls automatically.
