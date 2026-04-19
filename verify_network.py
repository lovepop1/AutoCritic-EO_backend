import requests
import json

def test_cors_preflight():
    url = "http://localhost:8000/api/load_imagery"
    headers = {
        "Access-Control-Request-Method": "POST",
        "Origin": "http://localhost:8000"
    }
    response = requests.options(url, headers=headers)
    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" in response.headers
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    print("CORS Preflight Test: PASSED")

def test_multi_temporal_array():
    url = "http://localhost:8000/api/compute_mask"
    payload = {
        "file_list": ["url1", "url2"],
        "sensor": "Sentinel-2",
        "index_type": "NDWI"
    }
    response = requests.post(url, json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "data" in data
    assert "computed_masks" in data["data"]
    assert isinstance(data["data"]["computed_masks"], list)
    assert "trend_analysis" in data["data"]
    assert isinstance(data["data"]["trend_analysis"], str)
    print("Multi-Temporal Array Test: PASSED")

def test_adversarial_array():
    url = "http://localhost:8000/api/mock/adversarial_optical"
    payload = {
        "aoi": {
            "type": "Polygon",
            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
        },
        "date_range": ["2023-01-01", "2023-01-31"],
        "sensor": "Sentinel-2"
    }
    response = requests.post(url, json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "data" in data
    assert "file_list" in data["data"]
    assert isinstance(data["data"]["file_list"], list)
    assert len(data["data"]["file_list"]) > 0
    # Check for diverse anomaly strings
    anomalies = ["CLOUD_OBSCURED.png", "NODATA_STRIPES.png", "NDVI_EXCEEDS_1.png"]
    for anomaly in anomalies:
        assert anomaly in data["data"]["file_list"]
    print("Adversarial Array Test: PASSED")

if __name__ == "__main__":
    try:
        test_cors_preflight()
        test_multi_temporal_array()
        test_adversarial_array()
        print("All tests PASSED. API is ready for local orchestration.")
    except AssertionError as e:
        print(f"Test FAILED: {e}")
    except Exception as e:
        print(f"Error: {e}")