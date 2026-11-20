"""Batch export of four-step, 16-bit PNGs with a reproducible manifest."""

import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import numpy as np
from PIL import Image

from .simulation import Calibration, DEFAULT_CALIBRATION, DEFAULT_SEED, Disturbance, PERIODS, build_scene, quantize, render
from .specimens import catalog


STEPS = (0, 90, 180, 270)


class ExportCancelled(Exception):
    pass


def sequence_seed(base_seed, specimen_id, cycles):
    return int(np.random.SeedSequence([base_seed, specimen_id, cycles]).generate_state(1)[0])


def export_sequences(destination, specimens, periods, calibration, disturbance, seed, progress=None, cancelled=None):
    periods = tuple(periods)
    if not periods or len(periods) > 3 or len(set(periods)) != len(periods) or any(f not in range(1, 129) for f in periods):
        raise ValueError("请勾选一至三组不同的频率")
    if not specimens:
        raise ValueError("请指定至少一个物体")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    batch = destination / f"fringes_{timestamp}_{uuid4().hex[:8]}"
    manifest = {
        "resolution": [1024, 1024], "png_bits": 16, "steps_deg": STEPS,
        "cycles_across_projector_width": periods, "seed": seed, "reference_depth_mm": calibration.reference_camera_depth,
        "projector_reference_depth_mm": calibration.projector_reference_depth,
        "disturbance": disturbance.parameters(), "calibration": calibration.data, "objects": [],
    }
    with TemporaryDirectory(dir=destination, prefix=".fringes-in-progress-") as scratch:
        staging = Path(scratch)
        for i, specimen in enumerate(specimens, start=1):
            if cancelled and cancelled():
                raise ExportCancelled()
            scene = build_scene(specimen, calibration)
            object_data = asdict(specimen)
            object_data["sequences"] = []
            for cycles in periods:
                if cancelled and cancelled():
                    raise ExportCancelled()
                run_seed = sequence_seed(seed, specimen.id, cycles)
                images = quantize(render(scene, calibration, cycles, disturbance, run_seed)[0])
                group = Path(f"{specimen.id:03d}") / f"f{cycles:03d}"
                (staging / group).mkdir(parents=True)
                names = []
                for step, frame in zip(STEPS, images, strict=True):
                    path = group / f"step_{step:03d}.png"
                    Image.fromarray(frame).save(staging / path, compress_level=3)
                    names.append(path.as_posix())
                object_data["sequences"].append({"cycles": cycles, "seed": run_seed, "frames": names})
            manifest["objects"].append(object_data)
            if progress:
                progress(i, len(specimens), specimen.label)
        if cancelled and cancelled():
            raise ExportCancelled()
        (staging / "仿真参数.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        staging.rename(batch)
    return batch


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--ids", type=int, nargs="+", help="Object IDs. Default: all catalog objects.")
    p.add_argument("--frequencies", type=int, nargs="+", default=PERIODS)
    p.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    for name in ("noise", "step-deg", "intensity-pct", "harmonic-pct", "quadrature-pct"):
        p.add_argument("--" + name, type=float, default=0.)
    args = p.parse_args()
    by_id = {item.id: item for item in catalog()}
    if args.ids and (not set(args.ids) <= by_id.keys() or len(set(args.ids)) != len(args.ids)):
        p.error("--ids 必须是图鉴中不同的物体编号")
    items = [by_id[i] for i in args.ids] if args.ids else list(by_id.values())
    limits = ((args.noise, 4), (args.step_deg, 5), (args.intensity_pct, 5),
              (args.harmonic_pct, 15), (args.quadrature_pct, 12))
    if args.seed < 0 or any(not (0 <= value <= upper) for value, upper in limits):
        p.error("种子与扰动参数须在有效范围内")
    disturbance = Disturbance(*(value for value, _ in limits))
    batch = export_sequences(args.output, items, args.frequencies, Calibration.read(args.calibration),
                             disturbance, args.seed, lambda done,total,label: print(f"{done}/{total} {label}", flush=True))
    print(batch)


if __name__ == "__main__":
    main()
