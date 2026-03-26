import csv
import os
import stat
import subprocess
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import QItemSelectionModel, QThread, QUrl, Qt, pyqtSignal, QSettings
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QMouseEvent, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from send2trash import send2trash

from core import DuplicateScanner, ScanOptions, ScanResult, effective_savings


class MultiSelectTable(QTableWidget):
    def mousePressEvent(self, e: Optional[QMouseEvent]):
        if e is None:
            return
        if e.button() == Qt.MouseButton.RightButton:
            index = self.indexAt(e.position().toPoint())
            if index.isValid():
                row = index.row()
                sm = self.selectionModel()
                if sm is None:
                    super().mousePressEvent(e)
                    return
                if sm.isRowSelected(row, index.parent()):
                    sm.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
                else:
                    self.clearSelection()
                    sm.select(
                        index,
                        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
                    )
                    sm.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
                e.accept()
                return
        super().mousePressEvent(e)


class ScanThread(QThread):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(object)

    def __init__(self, path: str, options: ScanOptions):
        super().__init__()
        self.path = path
        self.options = options

    def run(self):
        scanner = DuplicateScanner(self.path, options=self.options)

        def cb(step: int, total: int, msg: str):
            self.progress.emit(step, total, msg)

        result = scanner.scan(cb)
        self.finished.emit(result)


class ImageCompareDialog(QDialog):
    def __init__(self, left_path: str, right_path: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("图片对比预览")
        self.resize(1100, 700)

        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        left = self._make_view(left_path)
        right = self._make_view(right_path)
        root.addWidget(left)
        root.addWidget(right)

    def _make_view(self, path: str) -> QWidget:
        box = QWidget(self)
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        title = QLabel(path, box)
        title.setWordWrap(True)
        title.setStyleSheet("color:#333;")
        v.addWidget(title)

        scroll = QScrollArea(box)
        scroll.setWidgetResizable(True)
        inner = QLabel(scroll)
        inner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)

        pix = QPixmap(path)
        if not pix.isNull():
            inner.setPixmap(pix)
        else:
            inner.setText("无法加载图片")
        return box


class WxCleanerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WeChat File Cleaner (Linux)")
        self.resize(1200, 800)

        self.setFont(QFont("Sans Serif", 11))

        self.current_result: Optional[ScanResult] = None
        self.keep_paths: Set[str] = set()
        self.keep_for_path: Dict[str, str] = {}
        self.to_delete: Set[str] = set()
        self.scan_thread: Optional[ScanThread] = None

        self.rows_by_path_exact: Dict[str, List[int]] = defaultdict(list)
        self.rows_by_path_near: Dict[str, List[int]] = defaultdict(list)

        self.settings = QSettings("WeChatCleaner", "WxCleaner")
        
        self.setup_ui()
        
        last_path = self.settings.value("last_scan_path", "")
        if last_path:
            self.path_entry.setText(str(last_path))

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(14, 14, 14, 14)

        top = QHBoxLayout()
        top.addWidget(QLabel("微信文件路径:"))
        self.path_entry = QLineEdit()
        self.path_entry.setPlaceholderText("请选择或输入目录...")
            
        top.addWidget(self.path_entry)

        btn_browse = QPushButton("浏览")
        btn_browse.clicked.connect(self.browse_path)
        top.addWidget(btn_browse)

        self.btn_scan = QPushButton("开始扫描")
        self.btn_scan.clicked.connect(self.start_scan)
        self.btn_scan.setMinimumWidth(110)
        self.btn_scan.setStyleSheet("QPushButton{background:#1976D2;color:#fff;font-weight:600;border-radius:4px;padding:6px 10px;}QPushButton:hover{background:#1565C0;}")
        top.addWidget(self.btn_scan)
        root.addLayout(top)

        opt = QHBoxLayout()
        self.cb_parallel = QCheckBox("并行扫描")
        self.cb_parallel.setChecked(True)
        opt.addWidget(self.cb_parallel)

        opt.addWidget(QLabel("I/O 并发:"))
        self.cmb_io = QComboBox()
        self.cmb_io.addItems(["2", "4", "8"])
        self.cmb_io.setCurrentText("4")
        self.cmb_io.setFixedWidth(70)
        opt.addWidget(self.cmb_io)

        self.cb_img = QCheckBox("图片近似")
        self.cb_pdf = QCheckBox("PDF 忽略元数据")
        self.cb_office = QCheckBox("Office 结构归一")
        self.cb_archive = QCheckBox("压缩包归一")
        self.cb_img.setChecked(False)
        self.cb_pdf.setChecked(False)
        self.cb_office.setChecked(False)
        self.cb_archive.setChecked(False)
        opt.addWidget(self.cb_img)
        opt.addWidget(self.cb_pdf)
        opt.addWidget(self.cb_office)
        opt.addWidget(self.cb_archive)
        opt.addStretch()
        root.addLayout(opt)

        self.tabs = QTabWidget()
        self.table_exact = self._build_table()
        self.table_near = self._build_table()
        self.tabs.addTab(self.table_exact, "严格重复")
        self.tabs.addTab(self.table_near, "近似重复")
        self.tabs.currentChanged.connect(lambda _: self.update_summary())
        root.addWidget(self.tabs, 1)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        root.addWidget(self.progress)

        self.status = QLabel("就绪（Ctrl/Shift 多选；右键更多操作；双击预览）")
        self.status.setStyleSheet("color:#555;")
        root.addWidget(self.status)

        bottom = QHBoxLayout()
        btn_clear = QPushButton("清空结果")
        btn_clear.clicked.connect(self.clear_results)
        bottom.addWidget(btn_clear)

        btn_export = QPushButton("导出当前视图 CSV")
        btn_export.clicked.connect(self.export_current_view_csv)
        bottom.addWidget(btn_export)

        bottom.addStretch()

        btn_delete = QPushButton("移至回收站（待清理）")
        btn_delete.clicked.connect(self.delete_selected)
        btn_delete.setStyleSheet("QPushButton{background:#D32F2F;color:#fff;font-weight:600;border-radius:4px;padding:6px 14px;}QPushButton:hover{background:#C62828;}")
        bottom.addWidget(btn_delete)
        root.addLayout(bottom)

    def _build_table(self) -> QTableWidget:
        table = MultiSelectTable()
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(["分组", "文件名", "路径", "大小", "状态", "标记"])
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.doubleClicked.connect(lambda _: self.on_table_double_click(table))
        table.verticalHeader().setVisible(False)
        table.setShowGrid(False)
        table.setStyleSheet(
            "QTableWidget{background:#fff;border:1px solid #d0d0d0;}"
            "QTableWidget::item{padding:4px;border-bottom:1px solid #f0f0f0;}"
            "QTableWidget::item:selected{background:#BBDEFB;color:#0D47A1;}"
        )
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(lambda pos, t=table: self.show_context_menu(t, pos))
        return table

    def browse_path(self):
        path = QFileDialog.getExistingDirectory(self, "选择微信存储路径")
        if path:
            self.path_entry.setText(path)
            self.settings.setValue("last_scan_path", path)

    def build_options(self) -> ScanOptions:
        io_conc = int(self.cmb_io.currentText())
        return ScanOptions(
            parallel=bool(self.cb_parallel.isChecked()),
            io_concurrency=io_conc,
            sample_mode="head_tail",
            enable_image_perceptual=bool(self.cb_img.isChecked()),
            enable_pdf_normalize=bool(self.cb_pdf.isChecked()),
            enable_office_normalize=bool(self.cb_office.isChecked()),
            enable_archive_normalize=bool(self.cb_archive.isChecked()),
        )

    def start_scan(self):
        path = self.path_entry.text().strip()
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "错误", "请选择有效的路径！")
            return
            
        self.settings.setValue("last_scan_path", path)

        self.clear_results()
        self.btn_scan.setEnabled(False)
        self.btn_scan.setText("扫描中...")
        self.progress.setValue(0)

        options = self.build_options()
        self.scan_thread = ScanThread(path, options)
        self.scan_thread.progress.connect(self.update_progress)
        self.scan_thread.finished.connect(self.show_results)
        self.scan_thread.start()

    def update_progress(self, step: int, total: int, msg: str):
        if total > 0:
            self.progress.setValue(int((step / total) * 100))
        self.status.setText(msg)

    def clear_results(self):
        self.table_exact.setRowCount(0)
        self.table_near.setRowCount(0)
        self.current_result = None
        self.keep_paths = set()
        self.keep_for_path = {}
        self.to_delete = set()
        self.rows_by_path_exact = defaultdict(list)
        self.rows_by_path_near = defaultdict(list)
        self.status.setText("就绪（Ctrl/Shift 多选；右键更多操作；双击预览）")
        self.progress.setValue(0)

    def show_results(self, result: ScanResult):
        self.current_result = result
        exact = [g for g in result.groups if g.kind == "exact"]
        near = [g for g in result.groups if g.kind != "exact"]

        self.keep_paths = {g.keep_path for g in exact}
        self.keep_for_path = {}
        for g in exact:
            for p in g.deletable_paths():
                self.keep_for_path[p] = g.keep_path

        self.to_delete = set()
        for g in exact:
            self.to_delete.update(g.deletable_paths())

        self.rows_by_path_exact = defaultdict(list)
        self.rows_by_path_near = defaultdict(list)

        self._populate_table(self.table_exact, exact, self.rows_by_path_exact)
        self._populate_table(self.table_near, near, self.rows_by_path_near)

        self.progress.setValue(100)
        self.btn_scan.setEnabled(True)
        self.btn_scan.setText("开始扫描")
        self.update_summary()

    def _populate_table(self, table: QTableWidget, groups, rows_by_path: Dict[str, List[int]]):
        if not self.current_result:
            return
        rows = sum(len(g.paths) for g in groups)
        table.setRowCount(rows)
        row = 0
        for g in groups:
            inode_counts: Dict[Tuple[int, int], int] = defaultdict(int)
            for p in g.paths:
                fi = self.current_result.file_info.get(p)
                if fi:
                    inode_counts[(fi.dev, fi.ino)] += 1
            for p in g.paths:
                fi = self.current_result.file_info.get(p)
                if not fi:
                    continue
                size_str = self.format_size(fi.size)
                status_text, style_key = self.default_status_for_row(g, p)
                marker = self.marker_for_row(g, fi, inode_counts)
                group_label = self.group_label(g)
                meta = {"group_id": g.group_id, "kind": g.kind, "keep": g.keep_path, "path": p}
                self.insert_row(table, row, group_label, p, size_str, status_text, marker, style_key, meta)
                rows_by_path[p].append(row)
                row += 1

    def group_label(self, g) -> str:
        if g.kind == "exact":
            return f"组{g.group_id}"
        if g.kind == "near_pdf":
            return f"PDF近似{g.group_id}"
        if g.kind == "near_image":
            return f"图片近似{g.group_id}"
        if g.kind == "near_office":
            return f"Office近似{g.group_id}"
        return f"近似{g.group_id}"

    def default_status_for_row(self, g, path: str) -> Tuple[str, str]:
        if g.kind != "exact":
            if path in self.to_delete:
                return "删除", "delete"
            return "候选", "candidate"
        if path == g.keep_path:
            return "保留", "keep"
        if path in self.to_delete:
            return "删除", "delete"
        return "保留", "keep"

    def marker_for_row(self, g, fi, inode_counts: Dict[Tuple[int, int], int]) -> str:
        parts: List[str] = []
        if g.kind != "exact":
            parts.append("近似")
        if fi.nlink > 1:
            parts.append("硬链接")
        if inode_counts.get((fi.dev, fi.ino), 0) > 1:
            parts.append("同 inode")
        return " / ".join(parts)

    def insert_row(self, table: QTableWidget, row: int, group_name: str, filepath: str, size_str: str, status_text: str, marker: str, style_key: str, meta: dict):
        filename = os.path.basename(filepath)
        item_group = QTableWidgetItem(group_name)
        item_name = QTableWidgetItem(filename)
        item_path = QTableWidgetItem(filepath)
        item_size = QTableWidgetItem(size_str)
        item_status = QTableWidgetItem(status_text)
        item_marker = QTableWidgetItem(marker)

        item_path.setData(Qt.ItemDataRole.UserRole, meta)

        bg, fg = self.colors_for_style(style_key)
        for col, item in enumerate([item_group, item_name, item_path, item_size, item_status, item_marker]):
            item.setBackground(bg)
            item.setForeground(fg)
            if col in {0, 3, 4, 5}:
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            else:
                item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)

        table.setItem(row, 0, item_group)
        table.setItem(row, 1, item_name)
        table.setItem(row, 2, item_path)
        table.setItem(row, 3, item_size)
        table.setItem(row, 4, item_status)
        table.setItem(row, 5, item_marker)

    def colors_for_style(self, style_key: str) -> Tuple[QColor, QColor]:
        if style_key == "keep":
            return QColor("#F1F8E9"), QColor("#2E7D32")
        if style_key == "delete":
            return QColor("#FFEBEE"), QColor("#C62828")
        if style_key == "candidate":
            return QColor("#FFF8E1"), QColor("#EF6C00")
        if style_key == "deleted":
            return QColor("#ECEFF1"), QColor("#546E7A")
        return QColor("#FFFFFF"), QColor("#222222")

    def format_size(self, size_bytes: int) -> str:
        v = float(size_bytes)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if v < 1024.0:
                return f"{v:.2f} {unit}"
            v /= 1024.0
        return f"{v:.2f} PB"

    def table_selected_rows(self, table: QTableWidget) -> List[int]:
        rows = {idx.row() for idx in table.selectionModel().selectedRows()}
        return sorted(rows)

    def table_row_filepath(self, table: QTableWidget, row: int) -> Optional[str]:
        item = table.item(row, 2)
        if not item:
            return None
        return item.text()

    def table_row_meta(self, table: QTableWidget, row: int) -> Optional[dict]:
        item = table.item(row, 2)
        if not item:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def on_table_double_click(self, table: QTableWidget):
        r = table.currentRow()
        if r < 0:
            return
        fp = self.table_row_filepath(table, r)
        if fp:
            self.open_file(fp)

    def show_context_menu(self, table: QTableWidget, pos):
        row = table.rowAt(pos.y())
        if row < 0:
            return
        rows = self.table_selected_rows(table)
        if row not in rows:
            table.selectRow(row)
            rows = self.table_selected_rows(table)
        table.selectionModel().setCurrentIndex(table.model().index(row, 0), QItemSelectionModel.SelectionFlag.NoUpdate)
        table.setFocus()

        fp = self.table_row_filepath(table, row)
        if not fp:
            return

        selected_paths: List[str] = []
        for r in rows:
            p = self.table_row_filepath(table, r)
            if p:
                selected_paths.append(p)

        meta = self.table_row_meta(table, row) or {}
        kind = meta.get("kind")
        keep = meta.get("keep")

        menu = QMenu(self)
        a_open = menu.addAction("打开文件")
        a_dir = menu.addAction("打开所在文件夹")
        menu.addSeparator()

        a_compare = None
        if len(selected_paths) == 2:
            a_compare = menu.addAction("对比预览（所选 2 个）")

        a_add_delete = menu.addAction("加入清理列表（设为删除）")
        a_remove_delete = menu.addAction("从清理列表移除")
        a_delete_now = menu.addAction("移至回收站（仅所选）")
        menu.addSeparator()

        a_hardlink = None
        a_symlink = None
        if kind == "exact" and fp != keep:
            a_hardlink = menu.addAction("替换为硬链接（不删除入口）")
            a_symlink = menu.addAction("替换为软链接（不删除入口）")

        action = menu.exec(table.viewport().mapToGlobal(pos))
        if action == a_open:
            self.open_file(fp)
        elif action == a_dir:
            self.open_directory(fp)
        elif action == a_compare and len(selected_paths) == 2:
            self.compare_preview(selected_paths[0], selected_paths[1])
        elif action == a_add_delete:
            self.add_paths_to_delete(table, rows)
        elif action == a_remove_delete:
            self.remove_paths_from_delete(table, rows)
        elif action == a_delete_now:
            self.delete_paths(selected_paths, scope_label="所选")
        elif action == a_hardlink and kind == "exact":
            self.replace_with_hardlink(fp, keep)
        elif action == a_symlink and kind == "exact":
            self.replace_with_symlink(fp, keep)

    def compare_preview(self, a: str, b: str):
        ext_a = os.path.splitext(a)[1].lower()
        ext_b = os.path.splitext(b)[1].lower()
        image_ext = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
        if ext_a in image_ext and ext_b in image_ext:
            dlg = ImageCompareDialog(a, b, parent=self)
            dlg.exec()
            return
        self.open_file(a)
        self.open_file(b)

    def add_paths_to_delete(self, table: QTableWidget, rows: List[int]):
        for r in rows:
            meta = self.table_row_meta(table, r) or {}
            fp = meta.get("path") or self.table_row_filepath(table, r)
            if not fp:
                continue
            if fp in self.keep_paths:
                continue
            self.to_delete.add(fp)
            status_item = table.item(r, 4)
            if status_item:
                status_item.setText("删除")
            self.apply_row_style(table, r, "delete")
        self.update_summary()

    def remove_paths_from_delete(self, table: QTableWidget, rows: List[int]):
        for r in rows:
            meta = self.table_row_meta(table, r) or {}
            fp = meta.get("path") or self.table_row_filepath(table, r)
            if not fp:
                continue
            self.to_delete.discard(fp)
            kind = meta.get("kind")
            keep = meta.get("keep")
            status_item = table.item(r, 4)
            if not status_item:
                continue
            if kind == "exact":
                if fp == keep:
                    status_item.setText("保留")
                else:
                    status_item.setText("保留")
                self.apply_row_style(table, r, "keep")
            else:
                status_item.setText("候选")
                self.apply_row_style(table, r, "candidate")
        self.update_summary()

    def apply_row_style(self, table: QTableWidget, row: int, style_key: str):
        bg, fg = self.colors_for_style(style_key)
        for col in range(table.columnCount()):
            item = table.item(row, col)
            if item:
                item.setBackground(bg)
                item.setForeground(fg)

    def update_summary(self):
        savings = 0
        if self.current_result:
            savings = effective_savings(self.current_result.file_info, self.to_delete)
        exact_rows = self.table_exact.rowCount()
        near_rows = self.table_near.rowCount()
        self.status.setText(
            f"严格视图 {exact_rows} 行，近似视图 {near_rows} 行；当前待清理 {len(self.to_delete)} 个，预计释放 {savings / 1024 / 1024:.2f} MB"
        )

    def export_current_view_csv(self):
        table = self.table_exact if self.tabs.currentIndex() == 0 else self.table_near
        suggested = "exact.csv" if table is self.table_exact else "near.csv"
        path, _ = QFileDialog.getSaveFileName(self, "导出 CSV", suggested, "CSV Files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["group", "filename", "path", "size", "status", "marker", "kind", "keep"])
                for row in range(table.rowCount()):
                    p_item = table.item(row, 2)
                    meta = p_item.data(Qt.ItemDataRole.UserRole) if p_item else {}
                    group = table.item(row, 0).text() if table.item(row, 0) else ""
                    name = table.item(row, 1).text() if table.item(row, 1) else ""
                    p = table.item(row, 2).text() if table.item(row, 2) else ""
                    size = table.item(row, 3).text() if table.item(row, 3) else ""
                    status = table.item(row, 4).text() if table.item(row, 4) else ""
                    marker = table.item(row, 5).text() if table.item(row, 5) else ""
                    kind = meta.get("kind", "")
                    keep = meta.get("keep", "")
                    w.writerow([group, name, p, size, status, marker, kind, keep])
            QMessageBox.information(self, "导出完成", f"已导出：\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))

    def open_directory(self, filepath: str):
        d = os.path.dirname(filepath)
        if not os.path.exists(d):
            QMessageBox.warning(self, "错误", "文件夹不存在。")
            return
        self.open_path(d)

    def open_file(self, filepath: str):
        if not os.path.exists(filepath):
            QMessageBox.warning(self, "错误", "文件不存在，可能已删除。")
            return
        self.open_path(filepath)

    def open_path(self, path: str):
        try:
            ok = QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            if ok:
                return
            if sys.platform.startswith("linux"):
                subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
        except Exception as e:
            QMessageBox.warning(self, "错误", f"无法打开：\n{e}")

    def move_to_trash(self, filepath: str):
        try:
            send2trash(filepath)
            return
        except PermissionError:
            current_mode = os.stat(filepath).st_mode
            os.chmod(filepath, current_mode | stat.S_IWUSR)
            send2trash(filepath)

    def classify_delete_error(self, err: Exception) -> Tuple[str, str]:
        if isinstance(err, FileNotFoundError):
            return "not_found", "文件可能已被删除或移动，重新扫描即可"
        if isinstance(err, PermissionError):
            return "permission", "建议检查目录写权限/文件不可变属性（lsattr）/属主，或关闭微信占用后重试"
        if isinstance(err, OSError):
            return f"oserror_{getattr(err, 'errno', 'unknown')}", "建议查看错误码含义，常见为权限或跨文件系统限制"
        return "unknown", "建议打开所在文件夹后手工检查"

    def delete_selected(self):
        if not self.to_delete:
            QMessageBox.warning(self, "警告", "没有可清理的文件。")
            return
        self.delete_paths(sorted(self.to_delete), scope_label="待清理")

    def delete_paths(self, paths: List[str], scope_label: str):
        if not paths:
            QMessageBox.warning(self, "警告", "没有可清理的文件。")
            return
        reply = QMessageBox.question(
            self,
            "确认清理",
            f"确定要将 {len(paths)} 个文件移至回收站吗？\n此操作可在系统回收站恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        deleted = 0
        failures: List[Tuple[str, str]] = []
        cat_count: Dict[str, int] = defaultdict(int)
        cat_suggest: Dict[str, str] = {}

        for fp in paths:
            if fp in self.keep_paths:
                continue
            try:
                self.move_to_trash(fp)
                deleted += 1
                self.to_delete.discard(fp)
                self.mark_deleted(fp)
            except Exception as e:
                cat, sugg = self.classify_delete_error(e)
                cat_count[cat] += 1
                cat_suggest[cat] = sugg
                failures.append((fp, str(e)))

        if failures:
            sample = "\n".join([f"{p}\n  {msg}" for p, msg in failures[:8]])
            summary = "\n".join(
                [f"{k}: {v}（{cat_suggest.get(k,'')}）" for k, v in sorted(cat_count.items(), key=lambda x: (-x[1], x[0]))]
            )
            QMessageBox.warning(self, "部分失败", f"{scope_label}：成功 {deleted} 个，失败 {len(failures)} 个。\n\n分类：\n{summary}\n\n示例：\n{sample}")
        else:
            QMessageBox.information(self, "清理完成", f"{scope_label}：成功清理 {deleted} 个文件，已移至系统回收站。")

        self.update_summary()

    def mark_deleted(self, filepath: str):
        for row in self.rows_by_path_exact.get(filepath, []):
            status_item = self.table_exact.item(row, 4)
            if status_item:
                status_item.setText("已删除")
            self.apply_row_style(self.table_exact, row, "deleted")
        for row in self.rows_by_path_near.get(filepath, []):
            status_item = self.table_near.item(row, 4)
            if status_item:
                status_item.setText("已删除")
            self.apply_row_style(self.table_near, row, "deleted")

    def replace_with_hardlink(self, target_path: str, keep_path: str):
        if not keep_path or not os.path.exists(keep_path):
            QMessageBox.warning(self, "错误", "找不到保留文件。")
            return
        if not os.path.exists(target_path):
            QMessageBox.warning(self, "错误", "目标文件不存在。")
            return
        try:
            st_keep = os.stat(keep_path)
            st_target = os.stat(target_path)
            if st_keep.st_dev != st_target.st_dev:
                QMessageBox.warning(self, "错误", "硬链接要求在同一文件系统内。")
                return
            tmp = target_path + ".wxcleaner_tmp_link"
            if os.path.exists(tmp):
                os.remove(tmp)
            os.link(keep_path, tmp)
            self.move_to_trash(target_path)
            os.replace(tmp, target_path)
            self.to_delete.discard(target_path)
            self.refresh_row_after_link(target_path, marker="硬链接")
            self.update_summary()
        except Exception as e:
            QMessageBox.warning(self, "错误", f"替换为硬链接失败：\n{e}")

    def replace_with_symlink(self, target_path: str, keep_path: str):
        if not keep_path or not os.path.exists(keep_path):
            QMessageBox.warning(self, "错误", "找不到保留文件。")
            return
        if not os.path.exists(target_path):
            QMessageBox.warning(self, "错误", "目标文件不存在。")
            return
        try:
            tmp = target_path + ".wxcleaner_tmp_link"
            if os.path.lexists(tmp):
                os.remove(tmp)
            rel = os.path.relpath(keep_path, os.path.dirname(target_path))
            os.symlink(rel, tmp)
            self.move_to_trash(target_path)
            os.replace(tmp, target_path)
            self.to_delete.discard(target_path)
            self.refresh_row_after_link(target_path, marker="软链接")
            self.update_summary()
        except Exception as e:
            QMessageBox.warning(self, "错误", f"替换为软链接失败：\n{e}")

    def refresh_row_after_link(self, filepath: str, marker: str):
        for row in self.rows_by_path_exact.get(filepath, []):
            status_item = self.table_exact.item(row, 4)
            if status_item:
                status_item.setText("保留")
            marker_item = self.table_exact.item(row, 5)
            if marker_item:
                marker_item.setText(marker)
            self.apply_row_style(self.table_exact, row, "keep")


def run_app():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = WxCleanerApp()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run_app()
