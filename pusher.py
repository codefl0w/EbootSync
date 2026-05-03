# fl0w's PS Vita automatic EBOOT pusher daemon

import sys
import json
import time
import socket
import ftplib
import hashlib
import shutil
import zipfile
import tempfile
import winsound
from pathlib import Path
from datetime import datetime

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QProgressBar,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QFileDialog,
    QMessageBox,
    QSpinBox,
    QDoubleSpinBox,
    QCheckBox,
    QGroupBox,
)


APP_TITLE = "PS Vita EBOOT AutoSync"
DEFAULT_CONFIG_PATH = Path(__file__).with_name("vita_eboot_autosync_config.json")
DEFAULT_STATE_PATH = Path(__file__).with_name("vita_eboot_autosync_state.json")

def play_success_sound():
    try:
        winsound.Beep(1200, 150)
    except Exception:
        pass


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_eboot_from_vpk(vpk_path: Path) -> tuple[Path, Path]:
    temp_dir = Path(tempfile.mkdtemp(prefix="vita_eboot_push_"))
    temp_zip = temp_dir / "build.zip"
    extracted_eboot = temp_dir / "eboot.bin"

    shutil.copy2(vpk_path, temp_zip)

    with zipfile.ZipFile(temp_zip, "r") as zf:
        names = zf.namelist()

        eboot_name = None

        if "eboot.bin" in names:
            eboot_name = "eboot.bin"
        else:
            for name in names:
                if name.lower().endswith("/eboot.bin"):
                    eboot_name = name
                    break

        if not eboot_name:
            raise RuntimeError("eboot.bin not found inside VPK.")

        with zf.open(eboot_name) as src, extracted_eboot.open("wb") as dst:
            shutil.copyfileobj(src, dst)

    return temp_dir, extracted_eboot


class SyncWorker(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int)
    status_signal = pyqtSignal(str)
    started_signal = pyqtSignal()
    stopped_signal = pyqtSignal()
    config_error_signal = pyqtSignal(str)

    def __init__(self, config: dict, state_path: Path):
        super().__init__()
        self.config = config
        self.state_path = state_path
        self._stop_requested = False
        self._known_signature = None

    def stop(self):
        self._stop_requested = True
        self.log("Stop requested.")

    def log(self, message: str):
        self.log_signal.emit(f"[{now_str()}] {message}")

    def set_status(self, message: str):
        self.status_signal.emit(message)

    def set_progress(self, value: int):
        self.progress_signal.emit(value)

    def load_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as e:
            self.log(f"Failed to load state file: {e!r}")
            return {}

    def save_state(self, state: dict):
        try:
            self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception as e:
            self.log(f"Failed to save state file: {e!r}")

    def ftp_port_alive(self, host: str, port: int, timeout: float) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def ftp_login_check(self, host: str, port: int, timeout: float, remote_dir: str) -> bool:
        ftp = None
        try:
            ftp = ftplib.FTP()
            ftp.connect(host, port, timeout=timeout)
            ftp.login()
            ftp.cwd(remote_dir)
            ftp.quit()
            return True
        except Exception as e:
            self.log(f"FTP login check failed: {e!r}")
            try:
                if ftp is not None:
                    ftp.close()
            except Exception:
                pass
            return False

    def wait_until_file_stable(self, path: Path, stable_seconds: float) -> bool:
        self.log("Waiting for VPK to stabilize...")

        last_size = None
        last_mtime = None
        stable_since = None

        while not self._stop_requested:
            if not path.exists():
                self.log("VPK disappeared while waiting for stability.")
                return False

            try:
                stat = path.stat()
            except OSError as e:
                self.log(f"Stat failed while waiting for stability: {e!r}")
                time.sleep(0.5)
                continue

            size = stat.st_size
            mtime = stat.st_mtime

            if size == last_size and mtime == last_mtime:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= stable_seconds:
                    self.log("VPK is stable.")
                    return True
            else:
                last_size = size
                last_mtime = mtime
                stable_since = None

            time.sleep(0.5)

        return False

    def should_upload(self, local_file: Path) -> tuple[bool, dict]:
        state = self.load_state()

        temp_dir = None
        try:
            temp_dir, eboot_path = extract_eboot_from_vpk(local_file)

            eboot_hash = sha256_file(eboot_path)
            eboot_size = eboot_path.stat().st_size
            vpk_stat = local_file.stat()

            info = {
                "eboot_sha256": eboot_hash,
                "eboot_size": eboot_size,
                "vpk_size": vpk_stat.st_size,
                "vpk_mtime": vpk_stat.st_mtime,
            }

            if state.get("last_uploaded_eboot_sha256") == eboot_hash:
                return False, info

            return True, info

        finally:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def upload_file(self, host: str, port: int, timeout: float, local_file: Path, remote_dir: str, remote_name: str):
        temp_dir = None
        ftp = None

        try:
            self.set_progress(0)
            self.set_status("Extracting eboot.bin...")
            self.log("Copying VPK to temporary ZIP and extracting eboot.bin...")

            temp_dir, eboot_path = extract_eboot_from_vpk(local_file)

            total_size = eboot_path.stat().st_size
            sent = 0
            last_percent = -1
            temp_remote_name = remote_name + ".part"

            self.log(f"Extracted eboot.bin: {eboot_path}")
            self.log(f"eboot.bin size: {total_size} bytes")

            self.set_status("Uploading eboot.bin...")

            ftp = ftplib.FTP()
            ftp.connect(host, port, timeout=timeout)
            ftp.login()
            ftp.cwd(remote_dir)

            self.log(f"Connected to FTP. Uploading to temporary file: {temp_remote_name}")

            try:
                ftp.delete(temp_remote_name)
            except Exception:
                pass

            with eboot_path.open("rb") as f:
                def callback(chunk: bytes):
                    nonlocal sent, last_percent

                    if self._stop_requested:
                        raise RuntimeError("Upload stopped by user.")

                    sent += len(chunk)

                    if total_size > 0:
                        percent = int((sent / total_size) * 100)
                        if percent != last_percent:
                            last_percent = percent
                            self.set_progress(percent)

                ftp.storbinary(
                    f"STOR {temp_remote_name}",
                    f,
                    blocksize=64 * 1024,
                    callback=callback,
                )

            try:
                ftp.delete(remote_name)
            except Exception:
                pass

            ftp.rename(temp_remote_name, remote_name)

            try:
                remote_size = ftp.size(remote_name)
            except Exception:
                remote_size = None

            self.set_progress(100)
            self.log(f"Upload complete. Promoted {temp_remote_name} to {remote_name}. Remote size: {remote_size}")
            return remote_size

        finally:
            if ftp is not None:
                try:
                    ftp.quit()
                except Exception:
                    try:
                        ftp.close()
                    except Exception:
                        pass

            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def validate_config(self) -> tuple[bool, str]:
        try:
            local_file = Path(self.config["local_file"])
            host = self.config["host"].strip()
            port = int(self.config["port"])
            remote_dir = self.config["remote_dir"].strip()
            remote_name = self.config["remote_name"].strip()

            if not host:
                return False, "Host is empty."
            if port <= 0 or port > 65535:
                return False, "Port is invalid."
            if not local_file.exists():
                return False, f"Local VPK does not exist: {local_file}"
            if not local_file.is_file():
                return False, f"Local VPK path is not a file: {local_file}"
            if not remote_dir:
                return False, "Remote directory is empty."
            if not remote_dir.endswith("/"):
                return False, "Remote directory must end with '/'. Example: /ux0:/app/SIMS30001/"
            if not remote_name:
                return False, "Remote file name is empty."

            return True, ""
        except Exception as e:
            return False, f"Config validation failed: {e!r}"

    def run(self):
        valid, error = self.validate_config()
        if not valid:
            self.config_error_signal.emit(error)
            return

        self.started_signal.emit()
        self.set_status("Running")
        self.log("Watcher started.")

        host = self.config["host"].strip()
        port = int(self.config["port"])
        timeout = float(self.config["connect_timeout"])
        local_file = Path(self.config["local_file"])
        remote_dir = self.config["remote_dir"].strip()
        remote_name = self.config["remote_name"].strip()
        poll_interval = float(self.config["poll_interval"])
        stable_seconds = float(self.config["stable_seconds"])
        retry_delay = float(self.config["retry_delay"])
        infinite_retries = bool(self.config["infinite_retries"])
        max_retries = int(self.config["max_retries"])

        self.log(f"Watching VPK: {local_file}")
        self.log(f"Remote target: ftp://{host}:{port}{remote_dir}{remote_name}")

        while not self._stop_requested:
            if not local_file.exists():
                if self._known_signature is not None:
                    self.log("Watched VPK no longer exists.")
                    self._known_signature = None
                self.set_status("Waiting for VPK")
                time.sleep(poll_interval)
                continue

            try:
                stat = local_file.stat()
                signature = (stat.st_size, stat.st_mtime)
            except OSError as e:
                self.log(f"Stat failed: {e!r}")
                time.sleep(poll_interval)
                continue

            if signature != self._known_signature:
                self._known_signature = signature
                self.log("VPK change detected.")

                if not self.wait_until_file_stable(local_file, stable_seconds):
                    time.sleep(poll_interval)
                    continue

                try:
                    should_upload, info = self.should_upload(local_file)
                except Exception as e:
                    self.log(f"Failed to extract/hash eboot.bin: {e!r}")
                    time.sleep(poll_interval)
                    continue

                if not should_upload:
                    self.log("Extracted eboot.bin matches last uploaded version. Skipping.")
                    self.set_status("No upload needed")
                    time.sleep(poll_interval)
                    continue

                self.log("New eboot.bin detected. Starting pre-upload checks.")
                attempts = 0

                while not self._stop_requested:
                    attempts += 1
                    self.set_status("Checking FTP...")

                    if not self.ftp_port_alive(host, port, timeout):
                        self.log(f"FTP port not reachable at {host}:{port}.")
                    elif not self.ftp_login_check(host, port, timeout, remote_dir):
                        self.log("FTP service reachable, but login or cwd check failed.")
                    else:
                        try:
                            remote_size = self.upload_file(
                                host=host,
                                port=port,
                                timeout=timeout,
                                local_file=local_file,
                                remote_dir=remote_dir,
                                remote_name=remote_name,
                            )

                            self.save_state({
                                "last_uploaded_eboot_sha256": info["eboot_sha256"],
                                "last_uploaded_eboot_size": info["eboot_size"],
                                "last_vpk_size": info["vpk_size"],
                                "last_vpk_mtime": info["vpk_mtime"],
                                "last_upload_time": time.time(),
                                "last_remote_size": remote_size,
                                "local_file": str(local_file),
                                "remote_target": f"ftp://{host}:{port}{remote_dir}{remote_name}",
                            })

                            self.log("State saved. Sync successful.")
                            self.set_status("Sync successful")
                            play_success_sound()
                            break

                        except Exception as e:
                            self.log(f"Upload failed: {e!r}")
                            self.set_progress(0)
                            self.set_status("Upload failed")

                    if not infinite_retries and attempts >= max_retries:
                        self.log("Retry limit reached.")
                        break

                    self.log(f"Retrying in {retry_delay:.1f}s...")
                    slept = 0.0
                    while slept < retry_delay and not self._stop_requested:
                        time.sleep(0.2)
                        slept += 0.2

            time.sleep(poll_interval)

        self.set_status("Stopped")
        self.set_progress(0)
        self.log("Watcher stopped.")
        self.stopped_signal.emit()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(920, 680)

        self.worker = None

        self.host_edit = QLineEdit("192.168.1.108")

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(1337)

        self.local_file_edit = QLineEdit(
            r"C:\Users\fl0w\Desktop\port\TheSims3\dist\sims3_loader.vpk"
        )

        self.remote_dir_edit = QLineEdit("/ux0:/app/SIMS30001/")
        self.remote_name_edit = QLineEdit("eboot.bin")

        self.poll_interval_spin = QDoubleSpinBox()
        self.poll_interval_spin.setRange(0.2, 3600.0)
        self.poll_interval_spin.setDecimals(1)
        self.poll_interval_spin.setValue(2.0)

        self.stable_seconds_spin = QDoubleSpinBox()
        self.stable_seconds_spin.setRange(0.5, 3600.0)
        self.stable_seconds_spin.setDecimals(1)
        self.stable_seconds_spin.setValue(3.0)

        self.retry_delay_spin = QDoubleSpinBox()
        self.retry_delay_spin.setRange(0.5, 3600.0)
        self.retry_delay_spin.setDecimals(1)
        self.retry_delay_spin.setValue(5.0)

        self.connect_timeout_spin = QDoubleSpinBox()
        self.connect_timeout_spin.setRange(0.5, 120.0)
        self.connect_timeout_spin.setDecimals(1)
        self.connect_timeout_spin.setValue(5.0)

        self.infinite_retries_check = QCheckBox("Infinite retries")
        self.infinite_retries_check.setChecked(True)

        self.max_retries_spin = QSpinBox()
        self.max_retries_spin.setRange(1, 1000000)
        self.max_retries_spin.setValue(10)
        self.max_retries_spin.setEnabled(False)

        self.infinite_retries_check.toggled.connect(
            lambda checked: self.max_retries_spin.setEnabled(not checked)
        )

        self.status_label = QLabel("Idle")

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)

        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.save_button = QPushButton("Save Config")
        self.load_button = QPushButton("Load Config")
        self.browse_button = QPushButton("Browse...")

        self.stop_button.setEnabled(False)

        self.start_button.clicked.connect(self.start_worker)
        self.stop_button.clicked.connect(self.stop_worker)
        self.save_button.clicked.connect(self.save_config)
        self.load_button.clicked.connect(self.load_config)
        self.browse_button.clicked.connect(self.browse_local_file)

        self.init_ui()
        self.load_config(silent=True)

    def init_ui(self):
        config_group = QGroupBox("Configuration")
        config_layout = QGridLayout()

        row = 0
        config_layout.addWidget(QLabel("Host"), row, 0)
        config_layout.addWidget(self.host_edit, row, 1)
        config_layout.addWidget(QLabel("Port"), row, 2)
        config_layout.addWidget(self.port_spin, row, 3)

        row += 1
        config_layout.addWidget(QLabel("Local VPK"), row, 0)
        config_layout.addWidget(self.local_file_edit, row, 1, 1, 2)
        config_layout.addWidget(self.browse_button, row, 3)

        row += 1
        config_layout.addWidget(QLabel("Remote dir"), row, 0)
        config_layout.addWidget(self.remote_dir_edit, row, 1)
        config_layout.addWidget(QLabel("Remote name"), row, 2)
        config_layout.addWidget(self.remote_name_edit, row, 3)

        row += 1
        config_layout.addWidget(QLabel("Poll interval (s)"), row, 0)
        config_layout.addWidget(self.poll_interval_spin, row, 1)
        config_layout.addWidget(QLabel("Stable seconds"), row, 2)
        config_layout.addWidget(self.stable_seconds_spin, row, 3)

        row += 1
        config_layout.addWidget(QLabel("Retry delay (s)"), row, 0)
        config_layout.addWidget(self.retry_delay_spin, row, 1)
        config_layout.addWidget(QLabel("Connect timeout (s)"), row, 2)
        config_layout.addWidget(self.connect_timeout_spin, row, 3)

        row += 1
        config_layout.addWidget(self.infinite_retries_check, row, 0, 1, 2)
        config_layout.addWidget(QLabel("Max retries"), row, 2)
        config_layout.addWidget(self.max_retries_spin, row, 3)

        config_group.setLayout(config_layout)

        controls_layout = QHBoxLayout()
        controls_layout.addWidget(self.start_button)
        controls_layout.addWidget(self.stop_button)
        controls_layout.addWidget(self.save_button)
        controls_layout.addWidget(self.load_button)
        controls_layout.addStretch()
        controls_layout.addWidget(QLabel("Status:"))
        controls_layout.addWidget(self.status_label)

        progress_layout = QVBoxLayout()
        progress_layout.addWidget(QLabel("Upload progress"))
        progress_layout.addWidget(self.progress_bar)

        main_layout = QVBoxLayout()
        main_layout.addWidget(config_group)
        main_layout.addLayout(controls_layout)
        main_layout.addLayout(progress_layout)
        main_layout.addWidget(QLabel("Logs"))
        main_layout.addWidget(self.log_edit)

        self.setLayout(main_layout)

    def append_log(self, message: str):
        self.log_edit.append(message)

    def collect_config(self) -> dict:
        return {
            "host": self.host_edit.text().strip(),
            "port": int(self.port_spin.value()),
            "local_file": self.local_file_edit.text().strip(),
            "remote_dir": self.remote_dir_edit.text().strip(),
            "remote_name": self.remote_name_edit.text().strip(),
            "poll_interval": float(self.poll_interval_spin.value()),
            "stable_seconds": float(self.stable_seconds_spin.value()),
            "retry_delay": float(self.retry_delay_spin.value()),
            "connect_timeout": float(self.connect_timeout_spin.value()),
            "infinite_retries": bool(self.infinite_retries_check.isChecked()),
            "max_retries": int(self.max_retries_spin.value()),
        }

    def apply_config(self, config: dict):
        self.host_edit.setText(str(config.get("host", self.host_edit.text())))
        self.port_spin.setValue(int(config.get("port", self.port_spin.value())))
        self.local_file_edit.setText(str(config.get("local_file", self.local_file_edit.text())))
        self.remote_dir_edit.setText(str(config.get("remote_dir", self.remote_dir_edit.text())))
        self.remote_name_edit.setText(str(config.get("remote_name", self.remote_name_edit.text())))
        self.poll_interval_spin.setValue(float(config.get("poll_interval", self.poll_interval_spin.value())))
        self.stable_seconds_spin.setValue(float(config.get("stable_seconds", self.stable_seconds_spin.value())))
        self.retry_delay_spin.setValue(float(config.get("retry_delay", self.retry_delay_spin.value())))
        self.connect_timeout_spin.setValue(float(config.get("connect_timeout", self.connect_timeout_spin.value())))
        self.infinite_retries_check.setChecked(bool(config.get("infinite_retries", self.infinite_retries_check.isChecked())))
        self.max_retries_spin.setValue(int(config.get("max_retries", self.max_retries_spin.value())))

    def save_config(self):
        try:
            DEFAULT_CONFIG_PATH.write_text(
                json.dumps(self.collect_config(), indent=2),
                encoding="utf-8",
            )
            self.append_log(f"[{now_str()}] Config saved to {DEFAULT_CONFIG_PATH}")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", f"Failed to save config:\n{e!r}")

    def load_config(self, silent: bool = False):
        try:
            if not DEFAULT_CONFIG_PATH.exists():
                if not silent:
                    self.append_log(f"[{now_str()}] No config file found. Using UI defaults.")
                return

            config = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
            self.apply_config(config)

            if not silent:
                self.append_log(f"[{now_str()}] Config loaded from {DEFAULT_CONFIG_PATH}")

        except Exception as e:
            if not silent:
                QMessageBox.critical(self, "Load Error", f"Failed to load config:\n{e!r}")

    def browse_local_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select local VPK",
            self.local_file_edit.text() or str(Path.home()),
            "VPK files (*.vpk);;All files (*.*)",
        )

        if file_path:
            self.local_file_edit.setText(file_path)

    def set_controls_running(self, running: bool):
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)

        widgets = [
            self.host_edit,
            self.port_spin,
            self.local_file_edit,
            self.remote_dir_edit,
            self.remote_name_edit,
            self.poll_interval_spin,
            self.stable_seconds_spin,
            self.retry_delay_spin,
            self.connect_timeout_spin,
            self.infinite_retries_check,
            self.max_retries_spin,
            self.save_button,
            self.load_button,
            self.browse_button,
        ]

        for w in widgets:
            w.setEnabled(not running)

    def start_worker(self):
        if self.worker is not None and self.worker.isRunning():
            return

        config = self.collect_config()
        self.worker = SyncWorker(config=config, state_path=DEFAULT_STATE_PATH)

        self.worker.log_signal.connect(self.append_log)
        self.worker.progress_signal.connect(self.progress_bar.setValue)
        self.worker.status_signal.connect(self.status_label.setText)
        self.worker.started_signal.connect(lambda: self.set_controls_running(True))
        self.worker.stopped_signal.connect(lambda: self.set_controls_running(False))
        self.worker.config_error_signal.connect(self.on_config_error)

        self.progress_bar.setValue(0)
        self.status_label.setText("Starting...")
        self.worker.start()

    def stop_worker(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()

    def on_config_error(self, message: str):
        self.status_label.setText("Config error")
        self.progress_bar.setValue(0)
        self.set_controls_running(False)
        QMessageBox.critical(self, "Configuration Error", message)

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)

        event.accept()


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
