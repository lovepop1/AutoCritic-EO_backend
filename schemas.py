from pydantic import BaseModel
from typing import Dict, List, Literal, Optional, Union


class AOISchema(BaseModel):
    type: Literal["Polygon"]
    coordinates: List[List[List[float]]]


class AvailabilityRequest(BaseModel):
    aoi: Optional[AOISchema] = None
    location: Optional[str] = None
    date_range: Union[str, List[str]]
    sensor: Literal["Sentinel-1", "Sentinel-2", "optical", "SAR"] = "optical"


class AvailabilityData(BaseModel):
    images_found: int
    latest_pass_date: Optional[str] = None


class AvailabilityResponse(BaseModel):
    status: str = "success"
    data: AvailabilityData


# --- Structured metadata types ---

class CRSInfo(BaseModel):
    expected: str = "EPSG:4326"
    actual: str
    alignment: Literal["MATCH", "MISMATCH"]


class IndexStats(BaseModel):
    min: float
    max: float


class ImageryMetadata(BaseModel):
    cloud_cover_percent: float
    crs_info: CRSInfo
    index_stats: IndexStats
    timestamp: str  # ISO-8601
    anomaly_type: Optional[str] = None  # set to e.g. "INDEX_SCALING_ERROR" in adversarial mode


# --- Load Imagery ---

class LoadImageryRequest(BaseModel):
    aoi: Optional[AOISchema] = None
    location: Optional[str] = None
    date_range: Union[str, List[str]]
    sensor: Literal["Sentinel-1", "Sentinel-2", "optical", "SAR"] = "optical"
    bands: Optional[str] = None


class ImageryItem(BaseModel):
    asset_id: str
    thumbnail_url: str
    metadata: ImageryMetadata


class LoadImageryData(BaseModel):
    images: List[ImageryItem]


class LoadImageryResponse(BaseModel):
    status: str = "success"
    data: LoadImageryData


# --- Compute Mask ---

class ComputeMaskRequest(BaseModel):
    image_ids: Union[str, List[str]]
    sensor: Literal["Sentinel-1", "Sentinel-2", "optical", "SAR"] = "Sentinel-2"
    index_type: Literal["NDWI", "NBR", "NDVI", "Log-Ratio"]


class MaskItem(BaseModel):
    thumbnail_url: str
    metadata: ImageryMetadata


class ComputeMaskData(BaseModel):
    computed_masks: List[MaskItem]
    trend_analysis: str


class ComputeMaskResponse(BaseModel):
    status: str = "success"
    data: ComputeMaskData


class ErrorResponse(BaseModel):
    status: str = "error"
    message: str
