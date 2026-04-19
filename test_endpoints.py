from fastapi.testclient import TestClient
from main import app
import traceback

client = TestClient(app)

def run_tests():
    print("Testing /api/check_availability ...")
    try:
        response = client.post(
            "/api/check_availability",
            json={
                "aoi": {
                    "type": "Polygon",
                    "coordinates": [[[-122.092, 37.424], [-122.086, 37.424], [-122.086, 37.418], [-122.092, 37.418], [-122.092, 37.424]]]
                },
                "date_range": ["2023-01-01", "2023-01-31"],
                "sensor": "Sentinel-2"
            }
        )
        print(response.status_code, response.json())
    except Exception as e:
        traceback.print_exc()

    print("\nTesting /api/load_imagery ...")
    try:
        response = client.post(
            "/api/load_imagery",
            json={
                "aoi": {
                    "type": "Polygon",
                    "coordinates": [[[-122.092, 37.424], [-122.086, 37.424], [-122.086, 37.418], [-122.092, 37.418], [-122.092, 37.424]]]
                },
                "date_range": ["2023-01-01", "2023-01-31"],
                "sensor": "Sentinel-2"
            }
        )
        print(response.status_code, response.json())
    except Exception as e:
        traceback.print_exc()

    print("\nTesting /api/compute_mask ...")
    try:
        response = client.post(
            "/api/compute_mask",
            json={
                "file_list": ["https://dummy-url-1", "https://dummy-url-2"],
                "sensor": "Sentinel-2",
                "index_type": "NDWI"
            }
        )
        print(response.status_code, response.json())
    except Exception as e:
        traceback.print_exc()

if __name__ == "__main__":
    run_tests()
