"""Forward simulation for calibrated four-step fringe projection."""

from dataclasses import asdict, dataclass
from functools import cached_property
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter

from .geometry import grid_depth_buffer

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
BUNDLED_CALIBRATION = ROOT / "calibration" / "system.json"
LOCAL_CALIBRATION = ROOT / "calibration" / "local.json"
DEFAULT_CALIBRATION = LOCAL_CALIBRATION if LOCAL_CALIBRATION.is_file() else BUNDLED_CALIBRATION
PERIODS = (12, 19, 31)
REFERENCE_DEPTH = 420.0
DEFAULT_SEED = 1437
SHOT_COEFFICIENT = 1.5e-4
READ_VARIANCE = 4e-6
CORRELATED_READ_FRACTION = .12
TAU = 2 * np.pi


def distort(x, y, radial, tangential):
    k1, k2, k3 = (*radial, 0, 0, 0)[:3]
    p1, p2 = (*tangential, 0, 0)[:2]
    radius2 = x*x + y*y
    scale = 1 + radius2*(k1 + radius2*(k2 + radius2*k3))
    return (
        x*scale + 2*p1*x*y + p2*(radius2 + 2*x*x),
        y*scale + p1*(radius2 + 2*y*y) + 2*p2*x*y,
    )


def undistorted_rays(u, v, matrix, distortion):
    yd = (v - matrix[1, 2]) / matrix[1, 1]
    xd = (u - matrix[0, 2] - matrix[0, 1]*yd) / matrix[0, 0]
    x, y = xd.copy(), yd.copy()
    for _ in range(10):
        next_x, next_y = distort(x, y, *distortion)
        x += xd - next_x
        y += yd - next_y
    return x, y


def pixel_coordinates(points, matrix, distortion):
    x, y, z = np.moveaxis(points, -1, 0)
    dx, dy = distort(x/z, y/z, *distortion)
    u = matrix[0, 0]*dx + matrix[0, 1]*dy + matrix[0, 2]
    v = matrix[1, 1]*dy + matrix[1, 2]
    return u, v


class Calibration:
    def __init__(self, data, pose=0):
        if any(data[key].lower() not in ("mm", "millimeters", "millimetres")
               for key in ("worldUnits", "projectorWorldUnits")):
            raise ValueError("标定长度单位须为毫米")
        self.camera_k = np.asarray(data["cameraK"], dtype=float)
        self.projector_k = np.asarray(data["projectorK"], dtype=float)
        for matrix in (self.camera_k, self.projector_k):
            if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)) or min(matrix[0, 0], matrix[1, 1]) <= 0:
                raise ValueError("标定内参矩阵无效")
        self.camera_size = tuple(data["cameraImageSize"])
        self.projector_size = tuple(data["projectorImageSize"])
        self.camera_distortion = data["cameraRadial"], data["cameraTangential"]
        self.projector_distortion = data["projectorRadial"], data["projectorTangential"]
        camera_r = np.asarray(data["cameraRotationMatrices"], dtype=float).reshape(3, 3, -1)[:, :, pose]
        projector_r = np.asarray(data["projectorRotationMatrices"], dtype=float).reshape(3, 3, -1)[:, :, pose]
        camera_t = np.asarray(data["cameraTranslationVectors"], dtype=float).reshape(-1, 3)[pose]
        projector_t = np.asarray(data["projectorTranslationVectors"], dtype=float).reshape(-1, 3)[pose]
        self.rotation = projector_r @ camera_r.T
        self.translation = projector_t - self.rotation @ camera_t
        self.reference_camera_depth = float(camera_t[2]) if camera_t[2] > 0 else REFERENCE_DEPTH
        self.data = data

    @classmethod
    def read(cls, path=DEFAULT_CALIBRATION):
        return cls(json.loads(Path(path).read_text(encoding="utf-8-sig")))

    def rays(self, size):
        height, width = self.camera_size
        side = min(height, width)
        yy, xx = np.mgrid[:size, :size].astype(float)
        u = (xx + .5) * (side / size) + (width - side) / 2 + .5
        v = (yy + .5) * (side / size) + (height - side) / 2 + .5
        return undistorted_rays(u, v, self.camera_k, self.camera_distortion)

    @cached_property
    def projector_rays(self):
        v, u = np.indices(self.projector_size, dtype=float)
        return np.stack(undistorted_rays(u+1, v+1, self.projector_k, self.projector_distortion), axis=-1)

    @cached_property
    def projector_center(self):
        return -self.rotation.T @ self.translation

    @cached_property
    def projector_reference_depth(self):
        # Keep the board centre at the nominal camera depth, on the projector's
        # optical axis. The board lies in a plane of constant projector Z.
        forward = self.rotation[2, 2]
        if forward <= 0:
            raise ValueError("标定中的投影仪光轴未朝向仿真场景")
        distance = (self.reference_camera_depth - self.projector_center[2]) / forward
        if distance <= 0:
            raise ValueError("仿真参考面位于投影仪后方")
        return distance

    def projector_ray_z(self, rays):
        x, y = rays
        r = self.rotation[2]
        return r[0]*x + r[1]*y + r[2]

    def relative_height(self, camera_depth, rays):
        projector_depth = self.projector_ray_z(rays)*camera_depth + self.translation[2]
        return self.projector_reference_depth - projector_depth

    def project(self, ray_x, ray_y, depth):
        rotated = np.einsum("ij,j...->i...", self.rotation, [ray_x, ray_y, np.ones_like(ray_x)])
        shape = (3,) + (1,) * ray_x.ndim
        x, y, z = rotated * depth + self.translation.reshape(shape)
        dx, dy = distort(x/z, y/z, *self.projector_distortion)
        k = self.projector_k
        return k[0, 0]*dx + k[0, 1]*dy + k[0, 2], k[1, 1]*dy + k[1, 2], z

    def camera_pixels(self, points, size):
        u, v = pixel_coordinates(points, self.camera_k, self.camera_distortion)
        height, width = self.camera_size
        side = min(height, width)
        return (
            (u - (width-side)/2 - .5) * size/side - .5,
            (v - (height-side)/2 - .5) * size/side - .5,
        )


@dataclass(frozen=True)
class Scene:
    depth: np.ndarray
    mask: np.ndarray
    valid: np.ndarray
    background: np.ndarray
    modulation: np.ndarray
    projector_u: np.ndarray
    rays: tuple[np.ndarray, np.ndarray]


def fit_in_view(points, support, calibration, rays):
    """Frame each object in the shared view by translating and scaling X/Y."""
    size = support.shape[0]
    center_pixel = (size-1)/2
    indices = slice((size-1)//2, size//2+1)
    center_ray = np.array([component[indices, indices].mean() for component in rays] + [1.0])
    normal = calibration.rotation[2]
    z = (calibration.projector_reference_depth - calibration.translation[2]) / (normal @ center_ray)
    anchor = (calibration.rotation @ (center_ray*z) + calibration.translation)[:2]

    vertices = points[support]
    if not len(vertices):
        raise ValueError("当前相机采样下没有物体表面")
    stride = max(1, len(vertices)//6000)
    samples = vertices[::stride]
    scale = 1.0
    xy = anchor.copy()
    f_canvas = np.diag(calibration.camera_k)[:2] * size / min(calibration.camera_size)
    projector_half = np.array(calibration.projector_size[::-1], dtype=float)/2
    margin = 0.04*projector_half
    placed = samples.copy()
    for _ in range(6):
        placed[:, :2] = samples[:, :2]*scale + xy
        camera_xyz = (placed - calibration.translation) @ calibration.rotation
        camera_pixels = np.array(calibration.camera_pixels(camera_xyz, size))
        low, high = camera_pixels.min(axis=1), camera_pixels.max(axis=1)
        delta_px = center_pixel - (low + high)/2
        delta_camera = delta_px * np.median(camera_xyz[:, 2]) / f_canvas
        xy += calibration.rotation[:2, :2] @ delta_camera

        # Bound enlargement against the projector as well as the camera crop.
        placed[:, :2] = samples[:, :2]*scale + xy
        projector_pixels = np.array(pixel_coordinates(placed, calibration.projector_k, calibration.projector_distortion))
        lo, hi = projector_pixels.min(axis=1), projector_pixels.max(axis=1)
        room = projector_half - margin - np.abs((lo+hi)/2 - projector_half)
        safe_ratio = np.min(2*np.maximum(room, 1.)/np.maximum(hi-lo, 1.))
        scale *= max(.15, min(.87*size/max((high-low).max(), 1.), safe_ratio, 5.))

    points[..., :2] = points[..., :2]*scale + xy
    return points


def build_scene(specimen, calibration, size=1024):
    from .specimens import specimen_surface

    rays = calibration.rays(size)
    reference = calibration.reference_camera_depth
    height, support = specimen_surface(specimen, rays, reference)
    plane = calibration.projector_reference_depth
    front_view_depth = reference - height
    projector_points = np.stack((rays[0]*front_view_depth, rays[1]*front_view_depth, plane-height), axis=-1)
    if np.any(projector_points[..., 2][support] <= 0):
        raise ValueError("物体表面位于投影仪后方")
    projector_points = fit_in_view(projector_points, support, calibration, rays)
    camera_points = (projector_points - calibration.translation) @ calibration.rotation
    screen_u, screen_v = calibration.camera_pixels(camera_points, size)
    object_depth, normals = grid_depth_buffer(
        camera_points.reshape(-1, 3), np.stack((screen_u, screen_v), axis=-1).reshape(-1, 2),
        np.stack(rays, axis=-1), support, True,
    )
    ray_z = calibration.projector_ray_z(rays)
    board_depth = (plane - calibration.translation[2]) / ray_z
    mask = np.isfinite(object_depth) & (object_depth <= board_depth + 1e-5)
    if not mask.any():
        raise ValueError("当前标定下相机视场内没有物体")
    depth = np.where(mask, object_depth, board_depth)

    mesh_u, mesh_v = pixel_coordinates(projector_points, calibration.projector_k, calibration.projector_distortion)
    projector_depth, _ = grid_depth_buffer(
        projector_points.reshape(-1, 3), np.stack((mesh_u-1, mesh_v-1), axis=-1).reshape(-1, 2),
        calibration.projector_rays, support,
    )
    projector_depth = np.minimum(projector_depth, plane)
    u, v, z = calibration.project(*rays, depth)
    proj_h, proj_w = calibration.projector_size
    inside = (u >= .5) & (u <= proj_w+.5) & (v >= .5) & (v <= proj_h+.5) & (z > 0)
    ui = np.clip(np.floor(u-1).astype(np.int32), 0, proj_w-2)
    vi = np.clip(np.floor(v-1).astype(np.int32), 0, proj_h-2)
    # Use the farthest of the four neighbouring rays to avoid classifying
    # a sloped face as shadowed by other samples of that same face.
    blocking_depth = np.maximum.reduce((
        projector_depth[vi, ui], projector_depth[vi, ui+1],
        projector_depth[vi+1, ui], projector_depth[vi+1, ui+1],
    ))
    visible = inside & (z <= blocking_depth + .5)

    points = np.stack((rays[0]*depth, rays[1]*depth, depth), axis=-1)
    normals[~mask] = -calibration.rotation[2]
    light = calibration.projector_center - points
    light /= np.maximum(np.linalg.norm(light, axis=-1, keepdims=True), 1e-8)
    lambert = np.clip(np.sum(normals*light, axis=-1), 0, 1)
    lit = mask & visible
    if not lit.any():
        raise ValueError("当前标定下物体未受到投影照明")
    irradiance = np.clip(lambert / max(np.median(lambert[lit]), 1e-4), .15, 1.25)
    projected = irradiance * np.where(visible, 1, .08) * inside
    reflectance = np.where(mask, 1., .65)
    background = .012 + np.where(mask, .30, .12)*reflectance*projected
    modulation = np.where(mask, .22, .055)*reflectance*projected
    return Scene(depth, mask, lit, background, modulation, u, rays)


@dataclass(frozen=True)
class Disturbance:
    noise: float = 0
    step_deg: float = 0
    intensity_pct: float = 0
    harmonic_pct: float = 0
    quadrature_pct: float = 0

    def parameters(self):
        return asdict(self)


def render(scene, calibration, periods, disturbance=Disturbance(), seed=DEFAULT_SEED):
    """Render phase and digital sensor measurements in nominal step order."""
    proj_w = calibration.projector_size[1]
    ideal_phase = TAU * periods * (scene.projector_u - 1) / proj_w
    rng_step, rng_gain, rng_noise = (np.random.default_rng(child) for child in np.random.SeedSequence(seed).spawn(3))
    angle = ideal_phase[None, ...] + np.arange(4)[:, None, None] * np.pi / 2
    angle += np.deg2rad(disturbance.step_deg) * rng_step.normal(size=(4, 1, 1))
    cosine = np.cos(angle)
    q = disturbance.quadrature_pct / 100
    cosine[::2] *= 1 + q
    cosine[1::2] *= 1 - q
    frames = scene.background + scene.modulation * (
        cosine + disturbance.harmonic_pct / 100 * np.cos(3*angle)
    )
    if disturbance.intensity_pct:
        ratio = disturbance.intensity_pct / 100
        frames *= 1 + rng_gain.normal(0, ratio, size=(4, 1, 1))
        frames += rng_gain.normal(0, ratio * .15, size=(4, 1, 1))
    if disturbance.noise:
        signal = np.clip(frames, 0, 1)
        shot = SHOT_COEFFICIENT
        photon = rng_noise.poisson(signal / shot)*shot - signal
        white = rng_noise.normal(size=frames.shape)
        correlated = gaussian_filter(white, sigma=(0, 1.5, 1.5))
        correlated /= max(correlated.std(), 1e-9)
        read = np.sqrt(READ_VARIANCE)*(np.sqrt(1-CORRELATED_READ_FRACTION)*white + np.sqrt(CORRELATED_READ_FRACTION)*correlated)
        frames += disturbance.noise*(photon + read)
    return np.clip(frames, 0, 1), ideal_phase


def quantize(frames):
    return np.rint(np.clip(frames, 0, 1)*65535).astype(np.uint16)


def wrap(phase):
    return np.angle(np.exp(1j * phase))


def phase_from_frames(frames):
    f0, f90, f180, f270 = frames.astype(np.float64)
    return np.arctan2(f270 - f90, f0 - f180)
