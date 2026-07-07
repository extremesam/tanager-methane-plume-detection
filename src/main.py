"""
Tanager Methane Plume Detection Pipeline
=========================================
Connects to the Tanager STAC catalog, downloads a CH4 quicklook raster,
detects methane plumes via intensity thresholding, and exports geospatial
outputs (GeoTIFF mask + GeoJSON polygons) together with a PNG visualization.

Usage:
    python src/main.py
"""

import os
import json

import numpy as np
import matplotlib
matplotlib.use("Agg")          # non-interactive backend — saves PNG without a display
import matplotlib.pyplot as plt
import requests
import rasterio
from rasterio.features import shapes, geometry_mask
from shapely.geometry import shape, mapping
from pystac_client import Client


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CATALOG_URL   = "https://www.planet.com/data/stac/tanager-core-imagery/catalog.json"
DATA_DIR      = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

# Preferred asset keys in priority order (mask layers excluded deliberately)
PREFERRED_ASSETS = [
    "ortho_ql_ch4",   # methane quicklook — primary target
    "ortho_visual",   # RGB fallback
]

THRESHOLD   = 0.6   # normalised intensity above which a pixel is flagged as plume
MIN_AREA_PX = 50    # minimum polygon area (CRS units2) — filters out noise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _download(url: str, dest: str) -> None:
    """Stream-download *url* to *dest* in 8 KB chunks."""
    print(f"  Downloading -> {dest}")
    response = requests.get(url, stream=True)
    response.raise_for_status()
    downloaded = 0
    with open(dest, "wb") as fh:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                fh.write(chunk)
                downloaded += len(chunk)
    print(f"  Download complete — {downloaded / (1024 * 1024):.2f} MB")


def _normalize(array: np.ndarray) -> np.ndarray:
    """Return *array* linearly scaled to [0, 1]; NaN values are preserved."""
    valid = array[~np.isnan(array)]
    vmin, vmax = float(np.nanmin(valid)), float(np.nanmax(valid))
    if vmax > vmin:
        return (array - vmin) / (vmax - vmin)
    return np.zeros_like(array)


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def fetch_raster() -> str:
    """
    Navigate the Tanager STAC catalog and return the local path to the
    downloaded raster.  Skips the download if the file already exists.
    """
    print(f"[1/4] Connecting to catalog: {CATALOG_URL}")
    catalog = Client.open(CATALOG_URL)

    collections = list(catalog.get_collections())
    if not collections:
        raise RuntimeError("No collections found in catalog.")
    collection = collections[0]
    print(f"      Collection : {collection.id}  ({len(collections)} total)")

    items = list(collection.get_items())
    if not items:
        raise RuntimeError(f"No items found in collection '{collection.id}'.")
    item = items[0]
    print(f"      Item       : {item.id}  ({len(items)} total)")

    # Asset selection — priority list, no mask layers
    print(f"      Assets available: {list(item.assets.keys())}")
    asset_key = next((k for k in PREFERRED_ASSETS if k in item.assets), None)
    if asset_key is None:
        raise RuntimeError(
            f"None of the preferred assets {PREFERRED_ASSETS} were found. "
            f"Available: {list(item.assets.keys())}"
        )
    print(f"      Selected asset : {asset_key}")

    # Download
    os.makedirs(DATA_DIR, exist_ok=True)
    file_url  = item.assets[asset_key].get_absolute_href()
    filename  = f"{item.id}__{asset_key}.tif"
    filepath  = os.path.join(DATA_DIR, filename)

    if os.path.exists(filepath):
        print(f"      Cached file found — skipping download.")
    else:
        _download(file_url, filepath)

    return filepath, item.id


def inspect_and_detect(filepath: str):
    """
    Open the raster, print metadata + band statistics, normalise the CH4
    band, and apply the intensity threshold to produce a binary plume mask.

    Returns (src_meta, norm_display, mask, metrics).
    """
    print(f"\n[2/4] Loading raster: {filepath}")
    with rasterio.open(filepath) as src:
        print(f"      Band count : {src.count}")
        print(f"      Dimensions : {src.width} x {src.height}")
        print(f"      Data type  : {src.dtypes[0]}")
        print(f"      CRS        : {src.crs}")

        band = src.read(1).astype(float)
        if src.nodata is not None:
            band[band == src.nodata] = np.nan

        valid = band[~np.isnan(band)]
        b_min, b_max = float(np.nanmin(valid)), float(np.nanmax(valid))
        b_mean, b_std = float(np.nanmean(valid)), float(np.nanstd(valid))

        print(f"\n      Band 1 statistics")
        print(f"        min  : {b_min:.4f}")
        print(f"        max  : {b_max:.4f}")
        print(f"        mean : {b_mean:.4f}")
        print(f"        std  : {b_std:.4f}")

        norm         = _normalize(band)
        norm_display = np.clip(norm, 0, 1)
        mask         = norm > THRESHOLD

        total_px = band.size
        valid_px = int(np.sum(~np.isnan(band)))
        plume_px = int(np.sum(mask & ~np.isnan(band)))
        pct_valid = plume_px / valid_px * 100 if valid_px > 0 else 0.0
        pct_total = plume_px / total_px * 100 if total_px > 0 else 0.0

        print(f"\n      Plume detection (threshold={THRESHOLD})")
        print(f"        Total pixels       : {total_px:,}")
        print(f"        Valid pixels       : {valid_px:,}")
        print(f"        Plume pixels       : {plume_px:,}")
        print(f"        Coverage (of valid): {pct_valid:.2f}%")
        print(f"        Coverage (of total): {pct_total:.2f}%")

        src_meta  = src.meta.copy()
        transform = src.transform

    metrics = dict(
        total_px=total_px, valid_px=valid_px, plume_px=plume_px,
        pct_valid=pct_valid, pct_total=pct_total,
    )
    return src_meta, transform, norm_display, mask, metrics


def save_visualization(norm_display: np.ndarray, mask: np.ndarray,
                       item_id: str, metrics: dict) -> str:
    """Render the 3-panel detection figure and save it as a PNG."""
    print(f"\n[3/4] Saving visualization")
    pct_valid = metrics["pct_valid"]

    fig, axes = plt.subplots(1, 3, figsize=(18, 6))
    fig.suptitle(f"Methane Plume Detection — {item_id}", fontsize=13, fontweight="bold")

    # (A) Methane intensity heatmap
    im = axes[0].imshow(norm_display, cmap="inferno", interpolation="nearest")
    axes[0].set_title("Methane Intensity", fontsize=11)
    axes[0].axis("off")
    fig.colorbar(im, ax=axes[0], fraction=0.046, pad=0.04, label="Normalised intensity")

    # (B) Binary plume mask
    axes[1].imshow(mask, cmap="gray", interpolation="nearest")
    axes[1].set_title(f"Detected Plume Mask\n(threshold > {THRESHOLD})", fontsize=11)
    axes[1].axis("off")

    # (C) Overlay — intensity with plume pixels tinted in "cool"
    axes[2].imshow(norm_display, cmap="inferno", interpolation="nearest")
    masked_plume = np.where(mask, 1.0, np.nan)
    axes[2].imshow(masked_plume, cmap="cool", alpha=0.5, interpolation="nearest")
    axes[2].set_title(
        f"Plume Overlay\n({pct_valid:.2f}% of valid area flagged)", fontsize=11
    )
    axes[2].axis("off")

    plt.tight_layout()
    png_path = os.path.join(DATA_DIR, "plume_detection.png")
    plt.savefig(png_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"      Saved -> {png_path}")
    return png_path


def export_geospatial(
    src_meta: dict,
    transform,
    mask: np.ndarray,
    norm: np.ndarray,
) -> tuple:
    """
    Export two geospatial outputs:
      * data/plume_mask.tif   — uint8 GeoTIFF of the binary plume mask
      * data/plumes.geojson   — polygon FeatureCollection with per-plume
                                intensity metrics and severity classification
    """
    print(f"\n[4/4] Exporting geospatial outputs")

    # --- GeoTIFF mask ---
    mask_path = os.path.join(DATA_DIR, "plume_mask.tif")
    mask_meta = src_meta.copy()
    mask_meta.update({"dtype": "uint8", "count": 1, "nodata": 0})
    with rasterio.open(mask_path, "w", **mask_meta) as dst:
        dst.write(mask.astype(np.uint8), 1)
    print(f"      Mask GeoTIFF   -> {mask_path}")

    # --- Vectorise mask -> ranked Shapely polygons ---
    features = []
    category_counts = {"High": 0, "Medium": 0, "Low": 0}

    for geom_dict, _ in shapes(
        mask.astype(np.uint8),
        mask=(mask.astype(np.uint8) == 1),
        transform=transform,
    ):
        poly = shape(geom_dict)
        if poly.area < MIN_AREA_PX:   # suppress noise / artefacts
            continue

        # --- Per-polygon intensity metrics ---
        # Build a boolean mask the same shape as the raster that is True
        # only inside this polygon, then sample the normalised CH4 values.
        poly_mask = geometry_mask(
            [geom_dict],
            transform=transform,
            invert=True,           # True inside polygon
            out_shape=norm.shape,
        )
        values = norm[poly_mask]
        values = values[~np.isnan(values)]

        if values.size == 0:
            continue   # skip degenerate polygons with no valid pixels

        mean_intensity = float(values.mean())
        max_intensity  = float(values.max())

        # --- Severity classification ---
        if mean_intensity > 0.7:
            category = "High"
        elif mean_intensity > 0.5:
            category = "Medium"
        else:
            category = "Low"

        category_counts[category] += 1

        features.append({
            "type": "Feature",
            "geometry": mapping(poly),
            "properties": {
                "area":            round(poly.area, 4),
                "mean_intensity":  round(mean_intensity, 4),
                "max_intensity":   round(max_intensity, 4),
                "category":        category,
            },
        })

    # --- GeoJSON FeatureCollection ---
    geojson_path = os.path.join(DATA_DIR, "plumes.geojson")
    with open(geojson_path, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": features}, fh, indent=2)
    print(f"      Plume GeoJSON  -> {geojson_path}")
    print(f"      Plume polygons detected: {len(features)}")

    print("\n      Plume categories:")
    print(f"        High   : {category_counts['High']}")
    print(f"        Medium : {category_counts['Medium']}")
    print(f"        Low    : {category_counts['Low']}")

    return mask_path, geojson_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # fetch_raster() returns (filepath, item_id)
    filepath, item_id = fetch_raster()

    # inspect_and_detect() returns (src_meta, transform, norm_display, mask, metrics)
    src_meta, transform, norm_display, mask, metrics = inspect_and_detect(filepath)

    # save_visualization(norm_display, mask, item_id, metrics) -> saves PNG
    save_visualization(norm_display, mask, item_id, metrics)

    # export_geospatial(src_meta, transform, mask, norm) -> saves GeoTIFF + GeoJSON
    export_geospatial(src_meta, transform, mask, norm_display)

    print("\nPipeline complete.")


if __name__ == "__main__":
    main()
