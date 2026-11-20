"""The specimen catalog and camera-facing numerical surfaces, in millimetres."""

from dataclasses import dataclass
from functools import lru_cache
import json

import numpy as np
from PIL import Image

from .simulation import ASSETS


@dataclass(frozen=True)
class Specimen:
    id: int
    category: str
    name: str
    shape: str = ""
    variant: int = 0
    appearance: str = ""
    surface: str = ""
    height_scale_mm: float = 1.

    @property
    def label(self):
        return f"{self.id:03d} · {self.name}"


@lru_cache(maxsize=1)
def catalog():
    entries = json.loads((ASSETS / "catalog.json").read_text(encoding="utf-8"))
    return tuple(Specimen(**row) for row in entries)


def ellipse(x, y, a, b):
    return (x/a)**2 + (y/b)**2


def box(x, y, a, b):
    return (np.abs(x) <= a) & (np.abs(y) <= b)


def bump(r2):
    return np.sqrt(np.maximum(1-r2, 0.))


def load_object_surface(path):
    """Read an archived object's normalized height and Boolean support."""
    with np.load(path, allow_pickle=False) as source:
        shuffled = source["surface_xor"]
        residual = np.ascontiguousarray(np.moveaxis(shuffled, 0, -1)).view("<u4")[..., 0]
        recovered = np.bitwise_xor.accumulate(residual, axis=1, dtype="<u4").view("<f4")
        height = np.clip((recovered.astype(np.float64) - source["lo"]) / source["span"], 0, 1)
        return height, source["mask"].astype(bool, copy=False)


def specimen_surface(specimen, rays, reference):
    """Return masked height in millimetres on the output camera grid."""
    if not specimen.surface:
        return numerical_surface(specimen, rays, reference)

    native_height, mask = load_object_surface(ASSETS / specimen.surface)
    height = np.where(mask, native_height * specimen.height_scale_mm, 0).astype(np.float32)
    rows, cols = rays[0].shape
    if height.shape != (rows, cols):
        height = np.asarray(Image.fromarray(height).resize((cols, rows), Image.Resampling.BILINEAR))
        mask = np.asarray(Image.fromarray(mask).resize((cols, rows), Image.Resampling.NEAREST)).astype(bool)
    return height, mask


def numerical_surface(specimen, rays, reference):
    """Return surface height and silhouette on the supplied camera rays."""
    j = specimen.variant
    shape = specimen.shape
    ray_x, ray_y = rays
    size_factor = (.88, 1, 1.11)[j]
    angle = np.deg2rad((-10, 0, 13)[j])
    x = reference*(ray_x*np.cos(angle) + ray_y*np.sin(angle))/size_factor
    y = reference*(ray_y*np.cos(angle) - ray_x*np.sin(angle))/size_factor
    r, theta = np.hypot(x,y), np.arctan2(y,x)
    base = 3.5 + 1.2*j

    if shape in ("leaf_relief", "scallop_relief", "weave_relief", "scale_relief", "rosette",
                 "brick_relief", "diamond_relief", "bead_relief"):
        mask = box(x,y,25,22)
        spacing = 7+j*1.25
        gx = (x+spacing/2)%spacing-spacing/2
        gy = (y+spacing/2)%spacing-spacing/2
        if shape == "leaf_relief":
            blade = ellipse(x-.14*y,y,13+2*j,18)
            veins = np.exp(-.60*(x-.14*y)**2) + .44*np.exp(-.80*((y-.6*np.abs(x))%5-2.5)**2)
            raised = (3.3*bump(blade) + veins) * (blade<1)
        elif shape == "scallop_relief":
            fan_r = np.hypot(x,y-12)
            fan_angle = np.arctan2(x,12-y)
            raised = (2.1 + 1.5*np.cos((17+3*j)*fan_angle)) * bump((fan_r/32)**2) * (np.abs(fan_angle)<.94)
        elif shape == "weave_relief":
            crossing = np.cos(np.pi*x/spacing)*np.cos(np.pi*y/spacing)
            band_x, band_y = np.exp(-.45*gx**2), np.exp(-.45*gy**2)
            raised = (1.9+.7*crossing)*band_x+(1.9-.7*crossing)*band_y
        elif shape == "scale_relief":
            shifted = (gx + (np.floor((y+spacing/2)/spacing)%2)*spacing/2+spacing/2)%spacing-spacing/2
            raised = 3.4*bump(ellipse(shifted,gy+spacing*.14,spacing*.60,spacing*.66))
        elif shape == "rosette":
            petals = (1+np.cos((5+2*j)*theta))/2
            raised = 4.2*petals*np.exp(-.027*(r-12)**2) + 2.4*np.exp(-.17*r*r)
        elif shape == "brick_relief":
            offset = (np.floor((y+spacing/2)/spacing)%2)*(spacing*.80)
            horizontal = np.abs((x+offset+spacing*.8)%(spacing*1.6)-spacing*.8)
            raised = 3.1*(np.abs(gy)<spacing*.39)*(horizontal<spacing*.68)
        elif shape == "diamond_relief":
            diamond = np.abs(gx/spacing) + np.abs(gy/spacing)
            raised = 4*np.maximum(0,1-1.9*diamond)
        else:
            edge = np.minimum(25-np.abs(x),22-np.abs(y))
            raised = 3*np.exp(-.38*(edge-4.2)**2)*(1+.32*np.cos((x+y)*.71))
        height = base + raised
    elif shape == "sphere":
        radius, centre_z = (15., 20., 24.)[j], reference - 13.
        a = 1+ray_x**2+ray_y**2
        disc = centre_z**2-a*(centre_z**2-radius**2)
        depth = (centre_z-np.sqrt(np.maximum(disc,0)))/a
        height, mask = reference-depth, disc >= 0
    elif shape in ("flat", "tilt", "cuboid", "round"):
        mask = r < 23 if shape == "round" else box(x,y,25 if shape != "cuboid" else 20,21 if shape == "flat" else 18)
        height = np.full_like(x, base if shape in ("flat", "tilt") else 10.+4*j)
        if shape == "tilt":
            height += 7 + (.10 + .09*j)*x - .09*y
    elif shape == "dome":
        mask, height = r < 24, base + (11+3*j)*bump((r/24)**2)
    elif shape == "cylinder":
        mask = box(x,y,15,29)
        height = base + (11+2*j)*bump((x/15)**2)
    elif shape == "cone":
        mask, height = r < 24, base + np.minimum((24-r)*(.9+.11*j), 13+3*j)
    elif shape == "stair":
        mask = box(x,y,26,22)
        height = base + (np.floor(np.clip((x+26)/52,0,.999)*(j+3)))*(5-.6*j)
    elif shape == "terrace":
        mask = box(x,y,25,24)
        height = base + 7*box(x,y,19,18) + 8*box(x+2,y-1,10+j,9)
    elif shape == "wedge":
        mask, height = box(x,y,26,22), base + (x+26)*(.25+.05*j)
    elif shape == "l_block":
        mask = box(x,y,26,23) & ~((x>0)&(y<-3))
        height = base + 13*(x < -8+j)
    elif shape == "ridge":
        mask, height = box(x,y,24,27), base + 16*np.clip(1-np.abs(x+4*j-4)/16,0,1)
    elif shape == "chamfer":
        distance = np.minimum(24-np.abs(x),22-np.abs(y))
        mask, height = distance>=0, base+14*np.clip(distance/(3.+j),0,1)
    elif shape == "corner":
        mask = box(x,y,25,25) & ~((x<-8)&(y<-8))
        height = base + 12*((x>14-j)|(y>14-j)) + 3*(x>9+j)
    elif shape == "parallel":
        mask = box(x,y,25,20)
        height = base + 10*(x<-5-j) + 15*(x>5+j)
    elif shape in ("flange", "gear", "ring", "key", "impeller", "nozzle"):
        if shape == "flange":
            mask = (r<25)&(r>7+j)
            for phi in np.arange(4+j)*2*np.pi/(4+j):
                mask &= ellipse(x-16*np.cos(phi),y-16*np.sin(phi),3,3)>1
            height = base + 7*(r<12) + 2*(r>22)
        elif shape in ("gear", "ring"):
            lobes = np.clip(1.75*np.cos((10+4*j)*theta),-1,1)
            mask = ((r<23+2.7*lobes)&(r>6+j)) if shape == "gear" else ((r<25)&(r>15+2.2*lobes))
            height = base + 8 + 2*(r<10+j) + 1.5*lobes*(r>19)
        elif shape == "key":
            mask = (r<23)&((r>8+j)&~((x>0)&(x<13+j)&(np.abs(y)<2.8+j)))
            height = base + 8 + 4*(r>18)
        elif shape == "impeller":
            vane = np.maximum(np.cos((5+j)*(theta-.055*r)),0)**(4+j)
            mask, height = r<26, base + (12+2*j)*vane*bump((r/27)**2)+9*(r<6+j)
        else:
            mask = (r<26)&(r>6+j)
            height = base + (15+2*j)*np.exp(-.011*(r-7-j)**2)
    elif shape in ("slots", "channel", "housing", "perforated"):
        mask = box(x,y,25,23)
        if shape == "slots":
            slot = (np.abs(y)<16-2*j)&(np.abs((x+3)%(10-j)-(10-j)/2)<1.4+.3*j)
            height = base + 11*(~slot)
        elif shape == "channel":
            mask &= ~((np.abs(x)<6+2*j)&(y>0))
            height = base+13*(np.abs(x)>14)
        elif shape == "housing":
            mask &= (np.abs(x)+.65*np.abs(y)<36)
            height = base+16*(~box(x+2,y,17-j,13+j))+5*box(x-3,y+3,5,5)
        else:
            for row in range(3+j):
                for col in range(3):
                    mask &= ellipse(x-(col-1)*13,y-(row-(2+j)/2)*9,2+j*.35,2+j*.35)>1
            height = base+9+2*(np.abs(x)>21)
    elif shape in ("airfoil", "fan", "shell_panel"):
        spine = (3.5 + 1.4*j)*(y/32)**2 + (-.11+.12*j)*y
        chord = (16+j)*(1-.28*(y+32)/64)*bump((y/36)**6)
        cross = (x-spine)/np.maximum(chord,.01)
        mask = (np.abs(y)<33)&(np.abs(cross)<1)
        height = base+(11+3*j)*bump(cross**2)*(.78+.22*(1-y/33)**2) + 4*(1+cross)*(y+33)/66
        if shape == "fan":
            height = base + 5*bump(cross**2) + 12*(1+cross*(y/33))
        elif shape == "shell_panel":
            mask |= box(x+1,y-29,10,5)
            height += 4*(y>26)
    elif shape in ("saddle", "bowl", "spoon"):
        r2 = ellipse(x,y,25,22)
        mask = r2<1
        if shape == "saddle":
            height = base + 10 + (8+j)*((x/25)**2-(y/22)**2)+.14*x*y/22
        elif shape == "bowl":
            height = base + (14+2*j)*np.minimum(r2,1)
        else:
            bowl = ellipse(x,y+10,18,21)
            handle = ellipse(x,y-16,4+j*.7,23)
            mask = (bowl<1)|(handle<1)
            height = base + np.maximum(5*bump(handle), 14*np.minimum(bowl,1)*(bowl<1))
    elif shape == "hand":
        palm = ellipse(x,y-7,16+j,20)
        wrist = box(x,y-22,10,10)
        height = np.maximum((13+2*j)*bump(palm),8*wrist)
        mask = (palm<1)|wrist
        for index, (offset, tip) in enumerate(zip((-14,-9,-1,7,14),(-10,-23,-28,-24,-19),strict=True)):
            length = (7-tip)/2
            axis = offset + (.20+.05*j)*offset*np.clip(-(y+2)/27,0,1)
            finger = ellipse(x-axis,y-(7+tip)/2,4.9 if index==0 else 3.2+j*.28,length)
            mask |= finger<1
            height = np.maximum(height,(9+.7*index)*bump(finger))
        height += base
    elif shape == "dental":
        arch = np.hypot(x,y+3)
        mask = (ellipse(x,y,27,23)<1)&~((arch<14)&(y<5))
        height = np.full_like(x,base+3)
        for phi in np.linspace(.10,np.pi-.10,12+2*j):
            u,v = 21*np.cos(phi),14-31*np.sin(phi)
            enamel = ellipse(x-u,y-v,4.3,5.2)
            mask |= enamel<.82
            height += (5+1.7*np.sin(phi)**2)*bump(enamel)
    elif shape == "molar":
        mask = (ellipse(x,y,20,19)<1)|(ellipse(x+8,y-8,10,9)<1)
        occlusal = np.maximum.reduce([bump(ellipse(x-u,y-v,11+j,10)) for u,v in ((-8,-7),(8,-7),(-7,7),(7,7))])
        height = base+4+(17+2*j)*occlusal
    elif shape == "shell":
        radial = np.hypot(x,y-20)
        azimuth = np.arctan2(x,20-y)
        limit = 44-4*(azimuth**2)
        mask = (radial<limit)&(np.abs(azimuth)<.73+j*.07)
        height = base+(15+2*j)*bump((radial/46)**2)*np.cos(azimuth)**2 + 2.4*np.cos((20+3*j)*azimuth)*np.minimum(radial/17,1)
    elif shape in ("leaf","ear","foot","seed"):
        spine = (j-1)*.20*y + .0035*y*y
        scales = {"leaf":(15.,34.),"ear":(14.,25.),"foot":(11.,33.),"seed":(15.,31.)}
        a,b = scales[shape]
        r2 = ellipse(x-spine,y,a,b)
        mask = r2<1
        if shape == "leaf":
            veins = np.exp(-.41*(np.abs(x-spine)+.52*y%7-3.5)**2)
            height = base + 9*bump(r2)+2.4*np.exp(-.20*(x-spine)**2)+1.6*veins
        elif shape == "ear":
            height = base+7*bump(r2)+9*np.exp(-75*(r2-.55)**2)-4*np.exp(-.014*((x-3)**2+(y+3)**2))
        elif shape == "foot":
            y2 = y+27
            for k in range(5):
                mask |= ellipse(x-(k-2)*4,y2-k*.7,3.7-.24*k,6-.60*k)<1
            height = base+13*bump(r2)-4*np.exp(-.035*((x-4)**2+y*y))
        else:
            split = np.exp(-.38*(x-spine)**2)
            height = base + (17+2*j)*bump(r2)-4*split*bump(r2)
    else:
        raise ValueError(f"未知物体类型：{shape}")
    return np.maximum(height,0), mask
