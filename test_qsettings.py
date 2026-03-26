from PyQt6.QtCore import QSettings
import sys
from PyQt6.QtWidgets import QApplication

app = QApplication(sys.argv)
settings = QSettings("WeChatCleaner", "WxCleaner")
print(f"Current saved path: {settings.value('last_scan_path', '')}")
