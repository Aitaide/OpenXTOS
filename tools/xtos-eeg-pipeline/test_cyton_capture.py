import hashlib
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from cyton_capture import capture_fd, configure_serial_fd, open_serial_device
from test_openbci_cyton import packet


class CytonCaptureTests(unittest.TestCase):
    def test_configures_raw_115200_8n1_serial_without_flow_control(self):
        master, slave = pty.openpty()
        try:
            configure_serial_fd(slave)
            import termios
            attrs = termios.tcgetattr(slave)
            self.assertEqual(attrs[4], termios.B115200)
            self.assertEqual(attrs[5], termios.B115200)
            self.assertEqual(attrs[2] & termios.CSIZE, termios.CS8)
            self.assertTrue(attrs[2] & termios.CLOCAL)
            self.assertTrue(attrs[2] & termios.CREAD)
            self.assertFalse(attrs[2] & termios.PARENB)
            self.assertFalse(attrs[2] & termios.CSTOPB)
            self.assertEqual(attrs[6][termios.VMIN], 1)
            self.assertEqual(attrs[6][termios.VTIME], 0)
        finally:
            os.close(master)
            os.close(slave)

    def test_opens_character_device_and_rejects_symlink(self):
        master, slave = pty.openpty()
        try:
            path = os.ttyname(slave)
            fd = open_serial_device(path)
            os.close(fd)
            with tempfile.TemporaryDirectory() as temporary:
                link = Path(temporary) / "serial-link"
                link.symlink_to(path)
                with self.assertRaises(OSError):
                    open_serial_device(str(link))
        finally:
            os.close(master)
            os.close(slave)

    def test_captures_raw_bytes_frames_stats_and_hashes_from_pty(self):
        master, slave = pty.openpty()
        configure_serial_fd(slave)
        stop = threading.Event()
        result = {}
        errors = []
        packets = [packet(n, [n, -n, 0, 1, -1, 100, -100, 8388607])
                   for n in (17, 18, 20)]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"

            def run_capture():
                try:
                    result.update(capture_fd(slave, output, device_label="pty-fixture",
                                             duration_s=2.0, stop_event=stop))
                except BaseException as exc:
                    errors.append(exc)

            capture = threading.Thread(target=run_capture)
            capture.start()
            try:
                time.sleep(0.05)
                for raw in packets:
                    os.write(master, raw[:4])
                    os.write(master, raw[4:21])
                    os.write(master, raw[21:])
                time.sleep(0.1)
                stop.set()
                capture.join(timeout=3)
                self.assertFalse(capture.is_alive(), "capture thread did not stop")
                self.assertEqual(errors, [])
                raw_bytes = (output / "capture.bin").read_bytes()
                self.assertEqual(raw_bytes, b"".join(packets))
                frame_lines = (output / "frames.jsonl").read_text().splitlines()
                self.assertEqual(len(frame_lines), 4)
                header = json.loads(frame_lines[0])
                frames = [json.loads(line) for line in frame_lines[1:]]
                self.assertEqual(header["schema"], "xtos-openbci-cyton-capture-manifest-v1")
                self.assertEqual([frame["sample_number"] for frame in frames], [17, 18, 20])
                self.assertEqual([frame["sequence"] for frame in frames], [17, 18, 20])
                self.assertEqual(frames[0]["channels_nv"][0], 379)
                manifest = json.loads((output / "manifest.json").read_text())
                self.assertEqual(manifest["status"], "sample_gaps")
                self.assertIn("missing samples", manifest["capture_error"])
                self.assertEqual(manifest["stats"]["accepted_packets"], 3)
                self.assertEqual(manifest["stats"]["missing_samples"], 1)
                self.assertEqual(manifest["raw_capture_sha256"], hashlib.sha256(raw_bytes).hexdigest())
                frame_bytes = (output / "frames.jsonl").read_bytes()
                self.assertEqual(manifest["frames_sha256"], hashlib.sha256(frame_bytes).hexdigest())
                self.assertFalse(manifest["physical_device_evidence"])
            finally:
                stop.set()
                capture.join(timeout=3)
                os.close(master)
                os.close(slave)

    def test_rejects_capture_limit_above_replay_contract(self):
        master, slave = pty.openpty()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                with self.assertRaisesRegex(ValueError, "maximum capture capacity"):
                    capture_fd(slave, Path(temporary) / "capture",
                               device_label="pty-fixture", duration_s=0.1,
                               max_capture_bytes=17 * 1024 * 1024)
        finally:
            os.close(master)
            os.close(slave)

    def test_refuses_existing_output_directory(self):
        master, slave = pty.openpty()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "exists"
            output.mkdir()
            try:
                with self.assertRaises(FileExistsError):
                    capture_fd(slave, output, device_label="pty-fixture", duration_s=0.01)
                self.assertEqual(list(output.iterdir()), [])
            finally:
                os.close(master)
                os.close(slave)

    def test_stream_start_and_stop_commands_are_sent_and_stop_on_interrupt(self):
        master, slave = pty.openpty()
        configure_serial_fd(slave)
        stop = threading.Event()
        errors = []
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"

            def run_capture():
                try:
                    capture_fd(slave, output, device_label="pty-fixture", duration_s=2.0,
                               stop_event=stop, start_stream=True)
                except BaseException as exc:
                    errors.append(exc)

            capture = threading.Thread(target=run_capture)
            capture.start()
            try:
                readable, _, _ = __import__("select").select([master], [], [], 2)
                self.assertTrue(readable, "expected the Cyton start-stream command")
                self.assertEqual(os.read(master, 1), b"b")
                stop.set()
                capture.join(timeout=3)
                self.assertFalse(capture.is_alive())
                self.assertEqual(errors, [])
                readable, _, _ = __import__("select").select([master], [], [], 1)
                self.assertTrue(readable, "expected the Cyton stop-stream command")
                self.assertEqual(os.read(master, 1), b"s")
                manifest = json.loads((output / "manifest.json").read_text())
                self.assertEqual(manifest["stream_start_command"], "b")
                self.assertEqual(manifest["stream_stop_command"], "s")
            finally:
                stop.set()
                capture.join(timeout=3)
                os.close(master)
                os.close(slave)

    def test_device_eof_preserves_valid_and_trailing_partial_bytes(self):
        master, slave = pty.openpty()
        configure_serial_fd(slave)
        stop = threading.Event()
        errors = []
        raw_packet = packet(7, [7] * 8)
        trailing = packet(8, [8] * 8)[:9]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"

            def run_capture():
                try:
                    capture_fd(slave, output, device_label="pty-fixture", duration_s=2.0,
                               stop_event=stop)
                except BaseException as exc:
                    errors.append(exc)

            worker = threading.Thread(target=run_capture)
            worker.start()
            try:
                time.sleep(0.05)
                os.write(master, raw_packet + trailing)
                time.sleep(0.05)
                os.close(master)
                master = -1
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                manifest = json.loads((output / "manifest.json").read_text())
                self.assertLessEqual(manifest["raw_bytes"], len(raw_packet + trailing))
                self.assertEqual(manifest["stats"]["accepted_packets"], 1)
                self.assertEqual(manifest["status"], "incomplete_packet")
                self.assertEqual(manifest["trailing_packet_bytes"], len(trailing))
                self.assertIn("partial packet", manifest["capture_error"])
                self.assertEqual((output / "capture.bin").read_bytes(), raw_packet + trailing)
            finally:
                stop.set()
                worker.join(timeout=3)
                if master >= 0:
                    os.close(master)
                os.close(slave)

    def test_serial_noise_marks_capture_failed_even_when_valid_frames_follow(self):
        master, slave = pty.openpty()
        configure_serial_fd(slave)
        os.set_blocking(slave, False)
        stop = threading.Event()
        raw_packet = packet(30, [30] * 8)
        result = {}
        errors = []
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"

            def run_capture():
                try:
                    result.update(capture_fd(slave, output, device_label="pty-fixture",
                                             duration_s=2.0, stop_event=stop))
                except BaseException as exc:
                    errors.append(exc)

            worker = threading.Thread(target=run_capture)
            worker.start()
            try:
                time.sleep(0.03)
                os.write(master, b"noise" + raw_packet)
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(result["status"], "serial_noise")
                self.assertEqual(result["stats"]["accepted_packets"], 1)
                self.assertGreater(result["stats"]["discarded_bytes"], 0)
                self.assertIn("serial noise", result["capture_error"])
            finally:
                stop.set()
                worker.join(timeout=3)
                os.close(master)
                os.close(slave)

    def test_cli_opens_pty_streams_and_writes_capture_bundle(self):
        master, slave = pty.openpty()
        configure_serial_fd(slave)
        os.set_blocking(slave, False)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"
            command = [sys.executable, str(Path(__file__).with_name("cyton_capture.py")),
                       "--device", os.ttyname(slave), "--output", str(output),
                       "--duration-s", "0.3", "--start-stream"]
            process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                readable, _, _ = select.select([master], [], [], 3)
                self.assertTrue(readable, "CLI did not send the start command")
                self.assertEqual(os.read(master, 1), b"b")
                for counter in (40, 41, 42, 43):
                    os.write(master, packet(counter, [counter] * 8))
                    time.sleep(0.01)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(json.loads(stdout)["status"], "completed")
                manifest = json.loads((output / "manifest.json").read_text())
                self.assertEqual(manifest["stream_start_command"], "b")
                self.assertEqual(manifest["stream_stop_command"], "s")
                self.assertEqual(manifest["stats"]["accepted_packets"], 4)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=2)
                os.close(master)
                os.close(slave)

    def test_capacity_limit_publishes_bounded_partial_capture_with_failure_status(self):
        master, slave = pty.openpty()
        configure_serial_fd(slave)
        stop = threading.Event()
        errors = []
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "capture"

            def run_capture():
                try:
                    capture_fd(slave, output, device_label="pty-fixture", duration_s=0.5,
                               stop_event=stop, max_capture_bytes=2)
                except BaseException as exc:
                    errors.append(exc)

            capture = threading.Thread(target=run_capture)
            capture.start()
            try:
                time.sleep(0.05)
                os.write(master, packet(1, [0] * 8))
                capture.join(timeout=2)
                self.assertFalse(capture.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(output.is_dir())
                manifest = json.loads((output / "manifest.json").read_text())
                self.assertEqual(manifest["status"], "capacity_exceeded")
                self.assertLessEqual(manifest["raw_bytes"], 2)
                self.assertFalse(manifest["physical_device_evidence"])
            finally:
                stop.set()
                capture.join(timeout=2)
                os.close(master)
                os.close(slave)


if __name__ == "__main__":
    unittest.main()
