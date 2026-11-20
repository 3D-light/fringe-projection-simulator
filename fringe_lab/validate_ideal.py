"""Round-trip ideal 16-bit PNGs from multiple objects and score blind reconstruction."""

import argparse
from io import BytesIO
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .reconstruction import reconstruct, triangulate_columns
from .specimens import catalog
from .simulation import Calibration, DEFAULT_SEED, PERIODS, ROOT, build_scene, quantize, render, wrap


def png_round_trip(frame):
    stream = BytesIO()
    Image.fromarray(frame).save(stream, format="PNG")
    encoded = stream.getvalue()
    if (encoded[24], encoded[25]) != (16, 0):
        raise RuntimeError("PNG must store 16-bit grayscale samples")
    stream.seek(0)
    with Image.open(stream) as reopened:
        return np.asarray(reopened, dtype=np.uint16)


def check_object(source, calibration):
    scene = build_scene(source, calibration, size=1024)
    frames, phase_truth = [], []
    for period in PERIODS:
        ideal, truth = render(scene, calibration, period)
        frames.append(np.stack([png_round_trip(frame) for frame in quantize(ideal)]))
        phase_truth.append(truth)

    # Reconstruction receives recorded intensity, frequency and equipment
    # geometry. The generated scene enters only after the decoder has returned.
    result = reconstruct(frames, PERIODS, calibration)
    valid = scene.valid
    if not valid.any():
        raise RuntimeError(f"{source.label}: no directly illuminated object pixels")
    if not np.isfinite(result.depth_mm[valid]).all():
        raise RuntimeError(f"{source.label}: incomplete ideal reconstruction on the evaluation mask")
    phase_delta = wrap(result.wrapped[:, valid] - np.asarray(phase_truth)[:, valid])
    projected_delta = result.projector_u[valid] - scene.projector_u[valid]
    rays = tuple(component[valid] for component in scene.rays)
    expected_height = calibration.relative_height(scene.depth[valid], rays)
    height = calibration.relative_height(result.depth_mm[valid], rays)
    error = height - expected_height
    report = {
        "id": source.id,
        "object": source.label,
        "category": source.category,
        "object_pixels": int(scene.mask.sum()),
        "evaluated_pixels": int(valid.sum()),
        "object_pixels_with_depth": int(np.isfinite(result.depth_mm[scene.mask]).sum()),
        "wrapped_phase": {
            str(period): {"rmse_rad": float(np.sqrt(np.mean(delta**2))),
                          "max_abs_rad": float(np.max(np.abs(delta)))}
            for period, delta in zip(PERIODS, phase_delta, strict=True)
        },
        "projector_u_rmse_px": float(np.sqrt(np.mean(projected_delta**2))),
        "height_rmse_mm": float(np.sqrt(np.mean(error**2))),
        "height_max_abs_mm": float(np.max(np.abs(error))),
        "evaluation_mask_coverage": 1.0,
    }
    # Rounding either difference of two 16-bit samples can shift it by one
    # count. Bound the angle of that perturbation for each pixel's modulation.
    quadrature_counts = 2 * scene.modulation[valid] * np.iinfo(np.uint16).max
    phase_limit = np.arcsin(np.sqrt(2) / quadrature_counts) + 1e-11
    if np.any(np.abs(phase_delta) > phase_limit[None, :]):
        raise RuntimeError(f"Wrapped phase error exceeds the 16-bit rounding bound: {report}")
    if report["height_rmse_mm"] >= .005 or report["height_max_abs_mm"] >= .02:
        raise RuntimeError(f"Unexpected height error: {report}")
    return report


def check_depth_range(calibration):
    """Use additional camera rays to confirm the solver has no nominal depth range."""
    x, y = calibration.rays(96)
    result = []
    for depth in (320.0, 650.0):
        u, v, _ = calibration.project(x, y, depth)
        h, w = calibration.projector_size
        inside = (u >= 1) & (u < w) & (v >= 1) & (v < h)
        if not inside.any():
            raise RuntimeError(f"No projector overlap at camera depth {depth}")
        recovered = triangulate_columns(calibration, (x[inside], y[inside]), u[inside])
        error = float(np.max(np.abs(recovered - depth)))
        if not error < 1e-6:
            raise RuntimeError(f"Depth equation failed at {depth} mm: {error}")
        result.append({"depth_mm": depth, "max_abs_error_mm": error})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--per-category", type=int, help="Sample this many objects from each category")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--report", type=Path, default=ROOT / "docs" / "ideal_validation.json")
    options = parser.parse_args()
    objects = catalog()
    rng = np.random.default_rng(options.seed)
    if options.per_category is not None:
        categories = tuple(dict.fromkeys(item.category for item in objects))
        indices = []
        for category in categories:
            members = [i for i, item in enumerate(objects) if item.category == category]
            if not 0 < options.per_category <= len(members):
                parser.error(f"--per-category must be between 1 and {len(members)}")
            indices.extend(rng.choice(members, options.per_category, replace=False))
    else:
        if not 0 < options.count <= len(objects):
            parser.error(f"--count must be between 1 and {len(objects)}")
        indices = rng.choice(len(objects), options.count, replace=False)
    calibration = Calibration.read()
    results = []
    for index in sorted(indices):
        result = check_object(objects[index], calibration)
        results.append(result)
        print(result["object"], max(v["rmse_rad"] for v in result["wrapped_phase"].values()),
              result["height_rmse_mm"], result["height_max_abs_mm"], flush=True)
    report = {
        "seed": options.seed, "available_objects": len(objects), "periods": PERIODS,
        "resolution": [1024, 1024], "steps_deg": [0, 90, 180, 270], "png_bits": 16,
        "decoder_inputs": ["unsigned 16-bit PNG pixel values", "projector frequencies in cycles across the width",
                           "nominal four-step order", "calibration"],
        "extra_geometry_checks": check_depth_range(calibration), "results": results,
    }
    options.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
