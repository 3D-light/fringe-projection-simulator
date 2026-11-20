"""Qt interface for the fringe projection simulator."""

import argparse
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import sys
from threading import Event

import numpy as np
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QSlider,
    QProgressBar, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from .export import ExportCancelled, export_sequences, sequence_seed
from .specimens import catalog
from .simulation import (
    BUNDLED_CALIBRATION, DEFAULT_CALIBRATION, DEFAULT_SEED, LOCAL_CALIBRATION, PERIODS, Calibration, Disturbance,
    build_scene, quantize, render,
)

STYLE = """
QWidget {color:#172b3a; font-family:"Microsoft YaHei UI"; font-size:13px;}
QMainWindow, QWidget#shell {background:#edf2f5;}
QFrame#panel {background:#ffffff; border:1px solid #d5e0e7; border-radius:13px;}
QFrame#canvas {background:#101b25; border:1px solid #263745; border-radius:9px;}
QFrame#row {border-bottom:1px solid #e8eef2;}
QLabel#title {font-size:28px; font-weight:700;}
QLabel#section {font-size:16px; font-weight:700;}
QLabel#main {font-weight:700;}
QLabel#sub {color:#536877; font-size:12px;}
QLabel#badge {color:#075b56; background:#d6efea; border-radius:8px; padding:6px 11px; font-weight:700;}
QLabel#canvasText {color:#d5e3ed;}
QPushButton, QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {background:#f6f8fa; border:1px solid #c7d5de; border-radius:6px; padding:5px 8px; min-height:21px;}
QPushButton {padding:5px 13px; font-weight:600;}
QPushButton:hover {background:#e8f4f3; border-color:#70aaa4;}
QPushButton#primary {background:#08746c; color:white; border-color:#08746c; padding:6px 17px;}
QPushButton#primary:hover {background:#065f58;}
QPushButton#primary:disabled {background:#e6edf1; color:#627884; border-color:#d5e0e7;}
QPushButton:disabled {color:#80909a; background:#e6edf1; border-color:#d5e0e7;}
QCheckBox::indicator {width:17px; height:17px;}
QSlider::groove:horizontal {height:4px; background:#dce6ec; border-radius:2px;}
QSlider::handle:horizontal {background:#08746c; width:13px; margin:-5px 0; border-radius:6px;}
QSlider:disabled::handle:horizontal {background:#9aadb8;}
"""


@dataclass(frozen=True)
class Factor:
    name: str
    detail: str
    unit: str
    maximum: float
    initial: float


FACTORS = (
    Factor("强度噪声", "散粒噪声与读出噪声", "×", 4, .8),
    Factor("相移步长误差", "各帧相移偏差的标准差", "°", 5, .35),
    Factor("光强波动", "帧间增益波动的标准差", "%", 5, .30),
    Factor("非线性响应", "三次谐波与基波幅值之比", "%", 15, 2.50),
    Factor("正交不平衡", "两路正交分量的差动增益", "%", 12, 1.50),
)


def caption(text, role="sub"):
    label = QLabel(text)
    label.setObjectName(role)
    return label


def panel():
    frame = QFrame()
    frame.setObjectName("panel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(19, 14, 19, 14)
    layout.setSpacing(8)
    return frame, layout


class FringePreview(QLabel):
    def __init__(self):
        super().__init__("加载条纹", objectName="canvasText")
        self.setMinimumSize(280, 285)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setAlignment(Qt.AlignCenter)
        self.source_pixmap = QPixmap()

    def show_frame(self, frame, mask):
        rows, cols = np.nonzero(mask)
        if not rows.size:
            self.show_message("当前物体无可显示区域")
            return

        bounds = np.s_[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
        foreground = mask[bounds]
        gray = np.rint(frame[bounds] / 257).astype(np.uint8)
        image = np.empty((*gray.shape, 4), dtype=np.uint8)
        image[:, :, :3] = np.where(foreground, gray, 0)[:, :, None]
        image[:, :, 3] = np.where(foreground, 255, 0)
        height, width = gray.shape
        qimage = QImage(image.data, width, height, image.strides[0], QImage.Format_RGBA8888)
        self.source_pixmap = QPixmap.fromImage(qimage.copy())
        self.fit_frame()

    def show_message(self, message):
        self.source_pixmap = QPixmap()
        self.setText(message)

    def fit_frame(self):
        if not self.source_pixmap.isNull():
            pixel_ratio = self.devicePixelRatioF()
            target_size = self.contentsRect().size() * pixel_ratio
            fitted = self.source_pixmap.scaled(target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            fitted.setDevicePixelRatio(pixel_ratio)
            self.setPixmap(fitted)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_frame()


class ExportThread(QThread):
    progress = Signal(int, int, str)
    succeeded = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, parent, *settings):
        super().__init__(parent)
        self.settings = settings
        self.stop = Event()

    def run(self):
        try:
            folder = export_sequences(*self.settings, progress=self.progress.emit, cancelled=self.stop.is_set)
        except ExportCancelled:
            self.cancelled.emit()
        except (OSError, ValueError, RuntimeError) as exc:
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(str(folder))


class FringeWindow(QMainWindow):
    def __init__(self, calibration_path=DEFAULT_CALIBRATION):
        super().__init__()
        self.setWindowTitle("条纹投影扰动仿真")
        self.resize(1240, 840)
        self.setMinimumSize(1050, 775)
        self.sources = catalog()
        self.worker = None
        self.close_pending = False
        self.calibration_path = Path(calibration_path).resolve()
        self.calibration = Calibration.read(self.calibration_path)
        self.delay = QTimer(self, singleShot=True, interval=115)
        self.delay.timeout.connect(self.update_preview)
        self.factor_values = []

        shell = QWidget(objectName="shell")
        self.setCentralWidget(shell)
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(25, 21, 25, 16)
        layout.setSpacing(13)
        header = QHBoxLayout()
        title = QVBoxLayout()
        title.setSpacing(3)
        title.addWidget(caption("条纹投影扰动仿真", "title"))
        title.addWidget(caption("相机–投影仪标定 · 四步相移 · 相位与高度误差"))
        header.addLayout(title, 1)
        header.addWidget(caption("四步相移   /   0° · 90° · 180° · 270°", "badge"), alignment=Qt.AlignVCenter)
        layout.addLayout(header)

        main = QHBoxLayout()
        main.setSpacing(13)
        controls, control = panel()
        control.addWidget(caption("扰动设置", "section"))
        control.addWidget(caption("勾选要叠加的因素。滑块与数值输入同步。"))
        for idx, factor in enumerate(FACTORS):
            row = QFrame(objectName="row")
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 5, 0, 6)
            line.setSpacing(10)
            switch = QCheckBox()
            switch.setAccessibleName(factor.name)
            switch.setChecked(idx in (0, 2, 3))
            label = QVBoxLayout()
            label.setSpacing(2)
            label.addWidget(caption(factor.name, "main"))
            label.addWidget(caption(factor.detail))
            slider = QSlider(Qt.Horizontal)
            slider.setAccessibleName(factor.name + "滑块")
            slider.setRange(0, round(factor.maximum * 100))
            slider.setValue(round(factor.initial * 100))
            value = QDoubleSpinBox()
            value.setAccessibleName(factor.name + "数值")
            value.setRange(0, factor.maximum)
            value.setSingleStep(.05)
            value.setSuffix(" " + factor.unit)
            value.setFixedWidth(86)
            value.setValue(factor.initial)
            slider.setEnabled(switch.isChecked())
            value.setEnabled(switch.isChecked())
            switch.toggled.connect(slider.setEnabled)
            switch.toggled.connect(value.setEnabled)
            switch.toggled.connect(self.queue_preview)
            slider.valueChanged.connect(lambda n, spin=value: spin.setValue(n / 100))
            value.valueChanged.connect(lambda n, bar=slider: bar.setValue(round(n * 100)))
            value.valueChanged.connect(self.queue_preview)
            line.addWidget(switch)
            label_box = QWidget()
            label_box.setFixedWidth(186)
            label_box.setLayout(label)
            line.addWidget(label_box)
            line.addWidget(slider, 1)
            line.addWidget(value)
            self.factor_values.append((switch, value))
            control.addWidget(row)

        control.addSpacing(5)
        control.addWidget(caption("投影频率", "section"))
        control.addWidget(caption("在投影仪全宽上的周期数。可导出一组、两组或三组。"))
        frequencies = QHBoxLayout()
        self.period_boxes = []
        for idx, default in enumerate(PERIODS, start=1):
            group = QHBoxLayout()
            active = QCheckBox(f"频率 {idx}")
            active.setChecked(True)
            value = QSpinBox()
            value.setRange(1, 128)
            value.setValue(default)
            value.setAccessibleName(f"频率 {idx} 的周期数")
            active.toggled.connect(value.setEnabled)
            active.toggled.connect(self.sync_frequencies)
            value.valueChanged.connect(self.sync_frequencies)
            group.addWidget(active)
            group.addWidget(value)
            frequencies.addLayout(group, 1)
            self.period_boxes.append((active, value))
        control.addLayout(frequencies)

        seed_line = QHBoxLayout()
        seed_line.addWidget(caption("随机种子", "main"))
        self.seed = QSpinBox()
        self.seed.setRange(0, 2147483647)
        self.seed.setValue(DEFAULT_SEED)
        self.seed.valueChanged.connect(self.queue_preview)
        seed_line.addWidget(self.seed)
        seed_line.addStretch(1)
        self.reset = QPushButton("关闭全部扰动")
        self.reset.clicked.connect(self.clear_factors)
        seed_line.addWidget(self.reset)
        control.addSpacing(4)
        control.addLayout(seed_line)
        control.addStretch()

        preview, preview_layout = panel()
        preview_layout.addWidget(caption("单帧条纹预览", "section"))
        bar = QHBoxLayout()
        self.object_box, self.frequency_box, self.frame_box, self.mode_box = (QComboBox() for _ in range(4))
        objects = QHBoxLayout()
        self.category_box = QComboBox()
        self.category_box.addItem("全部类别")
        self.category_box.addItems(dict.fromkeys(item.category for item in self.sources))
        objects.addWidget(self.category_box, 1)
        objects.addWidget(self.object_box, 2)
        preview_layout.addLayout(objects)
        self.category_box.currentIndexChanged.connect(self.sync_objects)
        self.sync_objects()
        self.object_box.setCurrentIndex(1)
        for idx in range(4):
            self.frame_box.addItem(f"第 {idx+1} 帧")
        self.mode_box.addItems(["含扰动", "理想"])
        for combo in (self.object_box, self.frequency_box, self.frame_box, self.mode_box):
            combo.currentIndexChanged.connect(self.queue_preview)
        for combo in (self.frequency_box, self.frame_box, self.mode_box):
            bar.addWidget(combo, 1)
        preview_layout.addLayout(bar)
        self.canvas = QFrame(objectName="canvas")
        canvas_layout = QVBoxLayout(self.canvas)
        canvas_layout.setContentsMargins(14, 14, 14, 14)
        self.picture = FringePreview()
        canvas_layout.addWidget(self.picture)
        preview_layout.addWidget(self.canvas, 1)
        main.addWidget(controls, 49)
        main.addWidget(preview, 51)
        layout.addLayout(main, 1)

        paths, path_layout = panel()
        path_grid = QGridLayout()
        path_grid.setHorizontalSpacing(10)
        path_grid.setVerticalSpacing(5)
        self.calibration_field = QLineEdit()
        self.calibration_field.setReadOnly(True)
        self.show_calibration()
        self.output_field = QLineEdit()
        self.output_field.setPlaceholderText("请选择四步条纹 PNG 和参数文件的输出文件夹")
        calibration_button = QPushButton("选择标定")
        calibration_button.clicked.connect(self.choose_calibration)
        output_button = QPushButton("选择文件夹")
        output_button.clicked.connect(self.choose_output)
        path_grid.addWidget(caption("相机 / 投影仪标定", "main"), 0, 0)
        path_grid.addWidget(self.calibration_field, 0, 1)
        path_grid.addWidget(calibration_button, 0, 2)
        path_grid.addWidget(caption("批量输出", "main"), 1, 0)
        path_grid.addWidget(self.output_field, 1, 1)
        path_grid.addWidget(output_button, 1, 2)
        path_grid.setColumnStretch(1, 1)
        path_layout.addLayout(path_grid)
        bottom = QHBoxLayout()
        self.status = caption(f"{len(self.sources)} 个物体 · 按当前设置导出 16 位 PNG")
        bottom.addWidget(self.status)
        bottom.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(180)
        self.progress.hide()
        bottom.addWidget(self.progress)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.cancel_export)
        self.cancel_button.hide()
        bottom.addWidget(self.cancel_button)
        self.export_button = QPushButton("导出全部物体", objectName="primary")
        self.export_button.clicked.connect(self.start_export)
        bottom.addWidget(self.export_button)
        path_layout.addLayout(bottom)
        layout.addWidget(paths)

        self.sync_frequencies()
        self.frequency_box.setCurrentIndex(self.frequency_box.count() - 1)

    def sync_objects(self):
        previous = self.object_box.currentData()
        category = self.category_box.currentText()
        self.object_box.blockSignals(True)
        self.object_box.clear()
        for item in self.sources:
            if self.category_box.currentIndex() == 0 or item.category == category:
                self.object_box.addItem(item.label, item)
        index = self.object_box.findData(previous)
        self.object_box.setCurrentIndex(max(0, index))
        self.object_box.blockSignals(False)
        self.queue_preview()

    def disturbance(self):
        values = [value.value() if switch.isChecked() else 0 for switch, value in self.factor_values]
        return Disturbance(*values) if self.mode_box.currentIndex() == 0 else Disturbance()

    def start_export(self):
        periods = self.periods()
        if not periods or len(periods) != len(set(periods)):
            QMessageBox.warning(self, "检查投影频率", "请勾选一至三组不同的频率。")
            return
        destination = self.output_field.text().strip()
        if not destination:
            self.choose_output()
            destination = self.output_field.text().strip()
        if not destination:
            return
        self.worker = ExportThread(self, destination, self.sources, periods, self.calibration,
                                   self.disturbance(), self.seed.value())
        self.worker.progress.connect(self.report_export)
        self.worker.succeeded.connect(self.export_succeeded)
        self.worker.failed.connect(self.export_failed)
        self.worker.cancelled.connect(lambda: self.status.setText("已取消导出"))
        self.worker.finished.connect(self.finish_export)
        self.progress.setRange(0, len(self.sources))
        self.progress.setValue(0)
        self.progress.show()
        self.cancel_button.show()
        self.cancel_button.setEnabled(True)
        self.export_button.setEnabled(False)
        self.status.setText("正在生成四步条纹")
        self.worker.start()

    def report_export(self, done, total, name):
        self.progress.setValue(done)
        self.status.setText(f"已导出 {done}/{total} · {name}")

    def export_succeeded(self, folder):
        self.status.setText(f"已导出 {len(self.sources)} 个物体")
        QMessageBox.information(self, "导出完成", folder)

    def export_failed(self, reason):
        self.status.setText("导出失败")
        QMessageBox.warning(self, "导出失败", reason)

    def cancel_export(self):
        self.worker.stop.set()
        self.cancel_button.setEnabled(False)
        self.status.setText("正在取消")

    def finish_export(self):
        self.progress.hide()
        self.cancel_button.hide()
        self.export_button.setEnabled(True)
        self.worker.deleteLater()
        self.worker = None
        if self.close_pending:
            self.close()

    def closeEvent(self, event):
        if self.worker is not None:
            self.close_pending = True
            self.cancel_export()
            event.ignore()
            return
        super().closeEvent(event)

    def periods(self):
        return [value.value() for switch, value in self.period_boxes if switch.isChecked()]

    def sync_frequencies(self):
        previous = self.frequency_box.currentData()
        self.frequency_box.blockSignals(True)
        self.frequency_box.clear()
        for period in self.periods():
            self.frequency_box.addItem(f"{period} 周期", period)
        selected = self.frequency_box.findData(previous)
        self.frequency_box.setCurrentIndex(max(0, selected))
        self.frequency_box.blockSignals(False)
        self.queue_preview()

    def clear_factors(self):
        for switch, _ in self.factor_values:
            switch.setChecked(False)
        self.mode_box.setCurrentIndex(1)

    def choose_calibration(self):
        filename, _ = QFileDialog.getOpenFileName(self, "选择相机和投影仪标定", "", "联合标定文件 (*.json)")
        if filename:
            try:
                calibration = Calibration.read(filename)
            except (ValueError, OSError, KeyError, IndexError, TypeError) as exc:
                QMessageBox.warning(self, "无法读取标定", str(exc))
                return
            self.calibration = calibration
            self.calibration_path = Path(filename).resolve()
            self.show_calibration()
            self.preview_scene.cache_clear()
            self.queue_preview()

    def show_calibration(self):
        cal = self.calibration
        cam_h, cam_w = cal.camera_size
        proj_h, proj_w = cal.projector_size
        label = {
            LOCAL_CALIBRATION: "本机实测标定",
            BUNDLED_CALIBRATION: "示例标定",
        }.get(self.calibration_path, self.calibration_path.name)
        self.calibration_field.setText(f"{label} · 相机 {cam_w}×{cam_h} / 投影仪 {proj_w}×{proj_h}")
        self.calibration_field.setToolTip(str(self.calibration_path))

    def choose_output(self):
        directory = QFileDialog.getExistingDirectory(self, "选择输出文件夹", self.output_field.text())
        if directory:
            self.output_field.setText(directory)

    def queue_preview(self, *_):
        self.delay.start()

    @lru_cache(maxsize=2)
    def preview_scene(self, source, calibration):
        return build_scene(source, calibration)

    def update_preview(self):
        source, period = self.object_box.currentData(), self.frequency_box.currentData()
        if source is None or period is None:
            self.picture.show_message("请至少勾选一组频率")
            return
        try:
            self.render_preview(source, period)
        except (ValueError, OSError, TypeError) as exc:
            self.picture.show_message(str(exc))

    def render_preview(self, source, period):
        calibration = self.calibration
        scene = self.preview_scene(source, calibration)
        disturbance = self.disturbance()
        frame_seed = sequence_seed(self.seed.value(), source.id, period)
        frames, _ = render(scene, calibration, period, disturbance, frame_seed)
        frame = quantize(frames[self.frame_box.currentIndex()])
        self.picture.show_frame(frame, scene.mask)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--screenshot", type=Path)
    options = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(STYLE)
    window = FringeWindow(options.calibration)
    window.show()
    if options.screenshot:
        def save_screenshot():
            window.update_preview()
            window.grab().save(str(options.screenshot))
            app.quit()
        QTimer.singleShot(350, save_screenshot)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
