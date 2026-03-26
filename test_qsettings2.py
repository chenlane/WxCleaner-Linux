from PyQt6.QtCore import QSettings
import sys
from PyQt6.QtWidgets import QApplication

app = QApplication(sys.argv)
settings = QSettings("WeChatCleaner", "WxCleaner")
settings.setValue("last_scan_path", "/tmp/test")
settings.sync()
print(f"Current saved path: {settings.value('last_scan_path', '')}")
