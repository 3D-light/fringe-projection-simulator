"""Build the README appearance gallery from the project catalog."""

from collections import Counter
from html import escape

import numpy as np
from PIL import Image
from scipy.ndimage import maximum_filter

from .simulation import ASSETS, ROOT, Calibration, REFERENCE_DEPTH
from .specimens import catalog, numerical_surface


INTRO = r"""# 四步相移条纹投影仿真

在带相机和投影仪的虚拟场景中生成灰度条纹，用于观察成像扰动对包裹相位和重建高度的影响。内置 140 个样件。000–127 是原有的 64 个石膏物体和 64 个结构化表面。128–133 为六类几何示例，134–139 补齐原先的 23 个独立头像。

![仿真界面](docs/interface.png)

## 开始使用

安装 Python 3.10 或更新版本，下载仓库后进入项目目录。

**Windows PowerShell**

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m fringe_lab.gui
```

以后可以双击 `运行界面.cmd`。如果系统没有 `py` 命令，用 `python -m venv .venv` 创建环境。

**macOS / Linux**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m fringe_lab.gui
```

界面需要桌面环境。也可以直接用下面的命令行方式批量生成。

## 预览与导出

1. 选择物体类别和编号，再选择频率、帧号和预览模式。
2. 勾选要叠加的扰动，用滑块或数值框调整强度。切换到「理想」可对照原条纹。
3. 选择要生成的频率和输出目录，点击「导出全部物体」。每次生成一个新文件夹，界面可查看进度或取消。

| 扰动 | 数值含义 |
| --- | --- |
| 强度噪声 | 散粒噪声与读出噪声的缩放倍数 |
| 相移步长误差 | 各帧相移偏差的标准差（°） |
| 光强波动 | 帧间增益波动的标准差（%） |
| 非线性响应 | 三次谐波相对基波的幅度（%） |
| 正交不平衡 | 正交分量差动增益（%） |

默认在投影仪宽度上生成 12、19、31 个周期。每频四帧的标称相移依次为 0°、90°、180°、270°。预览按物体轮廓裁切，导出的灰度 PNG 保留完整的 1024×1024 视场和 16 位量化。输出目录使用图鉴编号，例如 `074/f031/step_090.png`。根目录的 `仿真参数.json` 记录频率、种子、扰动和标定。

几何真值为参考平面到模拟表面的轴向高度，示例参考距离为 420 mm。

### 命令行

在已激活的虚拟环境中运行：

```sh
python -m fringe_lab.export --output outputs --ids 0 64 128 --frequencies 12 19 31
python -m fringe_lab.validate_ideal --per-category 1
```

省略 `--ids` 导出全部样件。省略扰动参数生成理想条纹，具体选项见 `python -m fringe_lab.export --help`。

## 样件数据

样件清单在 `assets/catalog.json`。000–127 保留原数据编号。已有 17 个原头像包含在 000–063 中，另补齐 6 个。界面选择「原有石膏头像」可查看完整 23 个。

- `assets/objects/NNN.webp` 是无损外观图。
- `assets/objects/NNN.npz` 保存前景掩膜与可无损恢复的数值高度。
- 128–133 分别为自由曲面、薄壁叶片、孔槽与齿形、台阶与棱边、细节浮雕和生物形貌示例，各一件。程序按参数实时生成几何。

数值高度和掩膜为 1024×1024。可在项目目录用 Python 读回。外观图使用独立的画幅，软件中的条纹和掩膜使用相机采样网格。

```python
from fringe_lab.specimens import catalog, load_object_surface

height, mask = load_object_surface("assets/objects/000.npz")
height_mm = catalog()[0].height_scale_mm * height
```

`height` 是归一化数值，按样件的 `height_scale_mm` 换算为毫米。

## 示例标定

`calibration/system.json` 是可直接运行的虚拟标定。相机尺寸 1600×1200，投影仪尺寸 1280×800，长度单位为毫米。需要替换时，在界面选择结构相同的 JSON。若已有 MATLAB 的相机和投影仪标定对象，可以用 `calibration/export_matlab.m` 转为此格式。使用内置示例时直接运行 Python 程序即可。

理想条纹验证把 PNG 读回后再解相位和三角测量，以数值表面为基准计算误差。[查看验证记录](docs/ideal_validation.json)。

"""


def thumbnail(item, rays):
    if item.appearance:
        with Image.open(ASSETS / item.appearance) as source:
            picture = source.convert("RGBA")
            return picture.crop(picture.getbbox())

    height, mask = numerical_surface(item, rays, REFERENCE_DEPTH)
    height = np.where(mask, height, 0)
    dy, dx = np.gradient(height)
    normalization = np.sqrt(1+dx**2+dy**2)
    diffuse = np.clip((.80+.46*dx+.38*dy)/normalization, 0, 1)
    x, y = [ray*(REFERENCE_DEPTH-height) for ray in rays]
    points = [(x[mask], y[mask], height[mask], 120+132*diffuse[mask])]

    # Show the vertical walls at discontinuities in the oblique catalog view.
    # The measurement simulation itself still uses the camera-facing heights.
    standard_solids = {"flat", "tilt", "cuboid", "round", "cone", "cylinder"}
    if item.category in ("台阶与棱边", "孔槽与齿形") or item.shape in standard_solids:
        for axis in (0, 1):
            for direction in (-1, 1):
                neighbor = np.roll(height, direction, axis=axis)
                wall = mask & (height-neighbor>2.5)
                xwall = ((x+np.roll(x,direction,axis=axis))/2)[wall]
                ywall = ((y+np.roll(y,direction,axis=axis))/2)[wall]
                side_gray = np.full(wall.sum(), 150 + 35*(direction==1) + 14*axis)
                for level in np.linspace(0,1,45):
                    points.append((xwall, ywall, neighbor[wall] + level*(height-neighbor)[wall], side_gray))

    x, y, h, shade = [np.concatenate(values) for values in zip(*points, strict=True)]
    spin = np.deg2rad(16)
    yaw_x = np.cos(spin)*x - np.sin(spin)*y
    yaw_y = np.sin(spin)*x + np.cos(spin)*y
    tilt = np.deg2rad(31)
    y_screen = np.cos(tilt)*yaw_y - np.sin(tilt)*h
    near = np.sin(tilt)*yaw_y + np.cos(tilt)*h
    scale = min(224/max(np.ptp(yaw_x),1),158/max(np.ptp(y_screen),1))
    px = np.rint((yaw_x-yaw_x.min())*scale).astype(int)
    py = np.rint((y_screen-y_screen.min())*scale).astype(int)
    shades = np.zeros((py.max()+1,px.max()+1),np.uint8)
    front_to_back = np.argsort(near)
    shades[py[front_to_back],px[front_to_back]] = shade[front_to_back].astype(np.uint8)
    # Fill rasterization gaps no wider than one output sample.
    dilated = maximum_filter(shades, size=3)
    filled = np.where(shades!=0,shades,dilated)
    picture = Image.fromarray(filled).convert("RGBA")
    picture.putalpha(Image.fromarray(np.uint8(filled>0)*255))
    return picture


def main():
    items, calibration = catalog(), Calibration.read()
    categories = Counter(item.category for item in items)
    expected = list(range(len(items)))
    assert [item.id for item in items] == expected
    thumbnails = ROOT / "docs" / "objects"
    thumbnails.mkdir(exist_ok=True)
    gallery_rays = calibration.rays(832)
    for item in items:
        image = thumbnail(item, gallery_rays)
        image.thumbnail((154, 112), Image.Resampling.LANCZOS)
        padded = Image.new("RGB", (164, 124), "#243441")
        padded.paste(image, ((164-image.width)//2, (124-image.height)//2), image)
        padded.save(thumbnails / f"{item.id:03d}.png", optimize=True)

    parts = [INTRO, "\n"]
    parts.append("""## 物体图鉴

编号与 Qt 下拉框及导出目录一致。各类别展示实际使用的外观缩略图。点击缩略图可查看单个样件。
""")
    for category, count in categories.items():
        group = [item for item in items if item.category == category]
        parts.append(f"\n### {category}（{count} 个）\n\n")
        parts.append("| " + " | ".join(["外观"]*6) + " |\n")
        parts.append("| " + " | ".join([" :---:"]*6) + " |\n")
        for offset in range(0, len(group), 6):
            cells = []
            for item in group[offset:offset+6]:
                path = f"docs/objects/{item.id:03d}.png"
                label = escape(item.label)
                cells.append(f'<a href="{path}"><img src="{path}" alt="{label}" width="116"></a><br>{label}')
            cells += [""]*(6-len(cells))
            parts.append("| " + " | ".join(cells) + " |\n")
    (ROOT / "README.md").write_text("".join(parts), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
