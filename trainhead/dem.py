"""Elevation sampling.

Primary source: Copernicus GLO-30 (30 m) tiles from the AWS open data bucket.
Fallback: AWS Terrain Tiles in Terrarium PNG encoding.
Both are downloaded once per catalog build and cached.
"""
from __future__ import annotations

import io
import logging
import math
import time
from pathlib import Path
from typing import Callable

import numpy as np
import requests

log = logging.getLogger(__name__)

USER_AGENT = "trainhead/0.1 (personal hiking planner)"


class DemError(RuntimeError):
    pass


def _bilinear(arr: np.ndarray, rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
    h, w = arr.shape
    rows = np.clip(rows, 0, h - 1)
    cols = np.clip(cols, 0, w - 1)
    r0 = np.floor(rows).astype(int)
    c0 = np.floor(cols).astype(int)
    r1 = np.minimum(r0 + 1, h - 1)
    c1 = np.minimum(c0 + 1, w - 1)
    fr = rows - r0
    fc = cols - c0
    top = arr[r0, c0] * (1 - fc) + arr[r0, c1] * fc
    bottom = arr[r1, c0] * (1 - fc) + arr[r1, c1] * fc
    return top * (1 - fr) + bottom * fr


class DemSampler:
    def sample(self, lats, lons) -> np.ndarray:  # pragma: no cover - interface
        raise NotImplementedError


class FunctionDem(DemSampler):
    """For tests: elevation from a function of (lat, lon)."""

    def __init__(self, fn: Callable[[np.ndarray, np.ndarray], np.ndarray]):
        self.fn = fn

    def sample(self, lats, lons):
        return np.asarray(self.fn(np.asarray(lats, float), np.asarray(lons, float)), dtype=float)


class GridDem(DemSampler):
    """A set of north-up rasters, each with its own geographic bounds."""

    def __init__(self):
        # key -> (array, north, west, deg_per_row, deg_per_col)
        self.tiles: dict[tuple[int, int], tuple[np.ndarray, float, float, float, float]] = {}

    def add_tile(self, key, arr, north, west, deg_per_row, deg_per_col):
        self.tiles[key] = (arr.astype(np.float32), north, west, deg_per_row, deg_per_col)

    def tile_key(self, lat, lon):
        raise NotImplementedError

    def sample(self, lats, lons):
        lats = np.asarray(lats, dtype=float)
        lons = np.asarray(lons, dtype=float)
        out = np.full(lats.shape, np.nan)
        keys = self.tile_keys(lats, lons)
        for key in set(map(tuple, keys)):
            if key not in self.tiles:
                continue
            mask = (keys[:, 0] == key[0]) & (keys[:, 1] == key[1])
            arr, north, west, dr, dc = self.tiles[key]
            rows = (north - lats[mask]) / dr
            cols = (lons[mask] - west) / dc
            out[mask] = _bilinear(arr, rows, cols)
        return out

    def tile_keys(self, lats, lons):  # pragma: no cover - overridden
        raise NotImplementedError


class CopernicusDem(GridDem):
    """Copernicus GLO-30 DEM, 1x1 degree COG tiles."""

    BASE = "https://copernicus-dem-30m.s3.amazonaws.com"

    def __init__(self, cache_dir: str | Path, bbox, session: requests.Session | None = None):
        super().__init__()
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = session or requests.Session()
        west, south, east, north = bbox
        for lat in range(math.floor(south), math.floor(north) + 1):
            for lon in range(math.floor(west), math.floor(east) + 1):
                self._load(lat, lon)
        if not self.tiles:
            raise DemError("no Copernicus tiles could be loaded")

    @staticmethod
    def tile_name(lat: int, lon: int) -> str:
        ns = "N" if lat >= 0 else "S"
        ew = "E" if lon >= 0 else "W"
        return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"

    def tile_keys(self, lats, lons):
        return np.stack([np.floor(lats).astype(int), np.floor(lons).astype(int)], axis=1)

    def _load(self, lat: int, lon: int):
        import rasterio

        name = self.tile_name(lat, lon)
        path = self.cache_dir / f"{name}.tif"
        if not path.exists():
            url = f"{self.BASE}/{name}/{name}.tif"
            log.info("downloading %s", url)
            resp = self.session.get(url, timeout=300, headers={"User-Agent": USER_AGENT})
            if resp.status_code == 404:
                log.warning("tile %s not available", name)
                return
            resp.raise_for_status()
            path.write_bytes(resp.content)
        with rasterio.open(path) as ds:
            arr = ds.read(1).astype(np.float32)
            t = ds.transform
            # rasterio transform: x = c*t.a + t.c, y = r*t.e + t.f (t.e negative).
            # Shift by half a pixel so row/col indices refer to pixel centres.
            north = t.f + t.e / 2.0
            west = t.c + t.a / 2.0
            self.add_tile((lat, lon), arr, north, west, -t.e, t.a)


class TerrariumDem(GridDem):
    """AWS Terrain Tiles (Terrarium encoding) at a fixed zoom level."""

    URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"

    def __init__(self, cache_dir: str | Path, bbox, zoom: int = 12, session: requests.Session | None = None):
        super().__init__()
        from PIL import Image  # noqa: F401  (fail early if pillow is missing)

        self.zoom = zoom
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = session or requests.Session()
        west, south, east, north = bbox
        x0, y0 = self._tile_xy(north, west)
        x1, y1 = self._tile_xy(south, east)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                self._load(x, y)
        if not self.tiles:
            raise DemError("no terrain tiles could be loaded")

    def _tile_xy(self, lat, lon):
        n = 2 ** self.zoom
        x = int((lon + 180.0) / 360.0 * n)
        lat_r = math.radians(lat)
        y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
        return x, y

    def tile_keys(self, lats, lons):
        n = 2 ** self.zoom
        xs = np.floor((lons + 180.0) / 360.0 * n).astype(int)
        lat_r = np.radians(lats)
        ys = np.floor((1.0 - np.arcsinh(np.tan(lat_r)) / np.pi) / 2.0 * n).astype(int)
        return np.stack([xs, ys], axis=1)

    def sample(self, lats, lons):
        # Web Mercator tiles are not linear in latitude, so convert to pixel space directly.
        lats = np.asarray(lats, dtype=float)
        lons = np.asarray(lons, dtype=float)
        n = 2 ** self.zoom
        px = (lons + 180.0) / 360.0 * n * 256
        py = (1.0 - np.arcsinh(np.tan(np.radians(lats))) / np.pi) / 2.0 * n * 256
        out = np.full(lats.shape, np.nan)
        keys = np.stack([(px // 256).astype(int), (py // 256).astype(int)], axis=1)
        for key in set(map(tuple, keys)):
            if key not in self.tiles:
                continue
            mask = (keys[:, 0] == key[0]) & (keys[:, 1] == key[1])
            arr = self.tiles[key][0]
            cols = px[mask] - key[0] * 256 - 0.5
            rows = py[mask] - key[1] * 256 - 0.5
            out[mask] = _bilinear(arr, rows, cols)
        return out

    def _load(self, x, y):
        from PIL import Image

        path = self.cache_dir / f"terrarium_{self.zoom}_{x}_{y}.png"
        if not path.exists():
            url = self.URL.format(z=self.zoom, x=x, y=y)
            resp = self.session.get(url, timeout=60, headers={"User-Agent": USER_AGENT})
            if resp.status_code != 200:
                log.warning("terrain tile %s/%s failed: %s", x, y, resp.status_code)
                return
            path.write_bytes(resp.content)
            time.sleep(0.05)
        img = Image.open(io.BytesIO(path.read_bytes())).convert("RGB")
        rgb = np.asarray(img, dtype=np.float32)
        elev = rgb[:, :, 0] * 256.0 + rgb[:, :, 1] + rgb[:, :, 2] / 256.0 - 32768.0
        self.tiles[(x, y)] = (elev, 0.0, 0.0, 1.0, 1.0)


def open_dem(cache_dir: str | Path, bbox) -> DemSampler:
    """Copernicus first, Terrarium tiles if that fails."""
    try:
        return CopernicusDem(cache_dir, bbox)
    except Exception as exc:  # network or format problems
        log.warning("Copernicus DEM unavailable (%s); falling back to terrain tiles", exc)
        return TerrariumDem(cache_dir, bbox)
