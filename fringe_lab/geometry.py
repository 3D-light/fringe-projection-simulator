"""Rasterize the calibrated views of a gridded, camera-visible surface."""

from math import ceil, floor

import numpy as np
from numba import njit


@njit(cache=True)
def _triangle(camera, screen, rays, depth, incidence, a, b, c, near):
    ax, ay, az = camera[a]
    bx, by, bz = camera[b]
    cx, cy, cz = camera[c]
    if min(az, bz, cz) <= 0:
        return

    arx, ary = ax / az, ay / az
    brx, bry = bx / bz - arx, by / bz - ary
    crx, cry = cx / cz - arx, cy / cz - ary
    area = brx * cry - bry * crx
    if abs(area) < 1e-16:
        return

    u0, v0 = screen[a]
    u1, v1 = screen[b]
    u2, v2 = screen[c]
    # The exact pixel test is against inverse-distorted rays. The extra
    # pixel allows for curvature of the distorted triangle edges.
    first_col = max(0, ceil(min(u0, u1, u2)) - 1)
    last_col = min(depth.shape[1] - 1, floor(max(u0, u1, u2)) + 1)
    first_row = max(0, ceil(min(v0, v1, v2)) - 1)
    last_row = min(depth.shape[0] - 1, floor(max(v0, v1, v2)) + 1)

    if near:
        e1x, e1y, e1z = bx-ax, by-ay, bz-az
        e2x, e2y, e2z = cx-ax, cy-ay, cz-az
        nx = e1y*e2z-e1z*e2y
        ny = e1z*e2x-e1x*e2z
        nz = e1x*e2y-e1y*e2x
        norm = np.sqrt(nx*nx + ny*ny + nz*nz)
        if norm <= 1e-13:
            return
        # Grid winding initially points away from the projector.
        nx, ny, nz = -nx/norm, -ny/norm, -nz/norm

    for row in range(first_row, last_row+1):
        for col in range(first_col, last_col+1):
            rx, ry = rays[row, col]
            sx, sy = rx-arx, ry-ary
            wb = (sx*cry - sy*crx) / area
            wc = (brx*sy - bry*sx) / area
            wa = 1.0-wb-wc
            if min(wa, wb, wc) < -1e-10:
                continue
            z = 1.0/(wa/az + wb/bz + wc/cz)
            if z < depth[row, col]:
                depth[row, col] = z
                if near:
                    incidence[row, col, 0] = nx
                    incidence[row, col, 1] = ny
                    incidence[row, col, 2] = nz


@njit(cache=True)
def grid_depth_buffer(vertices, projected_pixels, rays, support, keep_normals=False):
    """Return the first surface hit at each pixel centre and optional normals."""
    rows, cols = support.shape
    depth = np.full(rays.shape[:2], np.inf)
    if keep_normals:
        normals = np.zeros((rays.shape[0], rays.shape[1], 3))
    else:
        normals = np.zeros((1, 1, 3))
    mask = support.ravel()
    for row in range(rows-1):
        for col in range(cols-1):
            a = row*cols+col
            b, c, d = a+1, a+cols, a+cols+1
            if mask[a] and mask[d]:
                if mask[b]:
                    _triangle(vertices, projected_pixels, rays, depth, normals, a, b, d, keep_normals)
                if mask[c]:
                    _triangle(vertices, projected_pixels, rays, depth, normals, a, d, c, keep_normals)
            elif mask[b] and mask[c]:
                if mask[a]:
                    _triangle(vertices, projected_pixels, rays, depth, normals, a, b, c, keep_normals)
                elif mask[d]:
                    _triangle(vertices, projected_pixels, rays, depth, normals, b, d, c, keep_normals)
    return depth, normals
