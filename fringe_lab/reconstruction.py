"""Recover phase and camera depth from recorded four-step sequences."""

from dataclasses import dataclass
from math import gcd

import numpy as np

from .simulation import TAU, distort, phase_from_frames


def unwrap_projector(phases, periods):
    """Determine the projector column by matching fringe orders across frequencies."""
    periods = np.asarray(periods, dtype=int)
    if len(periods) != 3 or len(np.unique(periods)) != 3 or min(periods) < 1 or gcd(*periods) != 1:
        raise ValueError("三频高度重建需要三个不同的正整周期数，且最大公约数为 1")
    high = np.argmax(periods)
    base = phases[high] / TAU % 1
    observed = phases / TAU
    best_cost = np.full_like(base, np.inf)
    position = np.empty_like(base)
    for order in range(periods[high]):
        candidate = (base + order) / periods[high]
        difference = periods[:, None] * candidate - observed
        difference -= np.rint(difference)
        cost = np.sum(difference**2, axis=0)
        accept = cost < best_cost
        position[accept] = candidate[accept]
        best_cost[accept] = cost[accept]
    return position


def triangulate_columns(calibration, rays, projector_u):
    """Intersect a camera ray with a distorted projector column without a depth bound."""
    # In the projector frame the camera ray is P = d * camera_Z + t.
    # Its normalized image point is affine in inverse projector depth,
    # so each observed column gives one scalar distortion equation.
    x, y = rays
    dx, dy, dz = calibration.rotation @ np.asarray([x, y, np.ones_like(x)])
    tx, ty, tz = calibration.translation
    fx, skew, cx = calibration.projector_k[0]
    _, fy, cy = calibration.projector_k[1]
    x0, y0 = dx / dz, dy / dz
    along_x, along_y = tx - x0*tz, ty - y0*tz
    pinhole_slope = fx*along_x + skew*along_y
    stable = (np.abs(dz) > 1e-9) & (np.abs(pinhole_slope) > 1e-6)
    inverse = np.divide(
        projector_u - (fx*x0 + skew*y0 + cx), pinhole_slope,
        out=np.zeros_like(x), where=stable,
    )

    def distort_along_ray(value):
        xd, yd = distort(x0 + along_x*value, y0 + along_y*value, *calibration.projector_distortion)
        return fx*xd + skew*yd + cx, fy*yd + cy

    # The pinhole solution initializes the distortion fit. No scene range,
    # neighboring depth, foreground information or reference plane is used.
    increment = 1e-7
    for _ in range(8):
        predicted, _ = distort_along_ray(inverse)
        plus, _ = distort_along_ray(inverse + increment)
        minus, _ = distort_along_ray(inverse - increment)
        slope = (plus - minus) / (2*increment)
        correction = np.divide(predicted - projector_u, slope, out=np.zeros_like(x),
                               where=np.abs(slope) > 1e-6)
        inverse -= correction

    predicted_u, predicted_v = distort_along_ray(inverse)
    camera_z = (np.divide(1, inverse, out=np.full_like(x, np.nan), where=inverse > 0) - tz) / dz
    height, width = calibration.projector_size
    valid = (
        stable & (camera_z > 0) & (np.abs(predicted_u-projector_u) < 1e-6)
        & (projector_u >= .5) & (projector_u <= width + .5)
        & (predicted_v >= .5) & (predicted_v <= height + .5)
    )
    return np.where(valid, camera_z, np.nan)


@dataclass(frozen=True)
class Reconstruction:
    wrapped: np.ndarray
    projector_u: np.ndarray
    depth_mm: np.ndarray


def reconstruct(frame_sets, periods, calibration):
    """Decode three frequencies using only the input PNG samples and calibration."""
    frames = np.asarray(frame_sets)
    if frames.dtype != np.uint16 or frames.ndim != 4 or frames.shape[:2] != (3, 4):
        raise ValueError("需要三组 16 位灰度条纹，每组按 0°、90°、180°、270° 排列")
    if frames.shape[2] != frames.shape[3]:
        raise ValueError("图像应为从原始相机中心裁剪并缩放得到的正方形")
    phases = np.stack([phase_from_frames(group) for group in frames])

    # With four steps, 2B is the length of the two-difference intensity vector.
    intensity = frames.astype(float)
    quadrature = np.hypot(intensity[:, 0] - intensity[:, 2], intensity[:, 3] - intensity[:, 1])
    illuminated = np.all(quadrature >= (2 * .012 * 65535), axis=0)
    position = unwrap_projector(phases[:, illuminated], periods)
    projector_u = np.full(illuminated.shape, np.nan)
    projector_u[illuminated] = 1 + calibration.projector_size[1] * position
    ray_x, ray_y = calibration.rays(frames.shape[2])
    depth = np.full(illuminated.shape, np.nan)
    depth[illuminated] = triangulate_columns(
        calibration, (ray_x[illuminated], ray_y[illuminated]), projector_u[illuminated],
    )
    return Reconstruction(phases, projector_u, depth)
