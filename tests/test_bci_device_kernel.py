"""Production BCI core controls and versioned device query behavior."""
from pathlib import Path
import unittest
from test_bci_lifetime import BCILifetimeTest


class DeviceControlTests(BCILifetimeTest):
    SOURCE = Path(__file__).resolve().parent / "bci_device_host/device_test.c"


# The base class contributes lifecycle methods; this executable only owns controls.
for _name in list(vars(BCILifetimeTest)):
    if _name.startswith("test_"):
        setattr(DeviceControlTests, _name, None)


def _case(name):
    def test(self):
        import subprocess
        result = subprocess.run([str(self.binary), name], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), name)
    return test


DeviceControlTests.test_config_unsupported = _case("config_unsupported")
DeviceControlTests.test_config_readback = _case("config_readback")
DeviceControlTests.test_config_invalid_request = _case("config_invalid_request")
DeviceControlTests.test_gain_not_silently_ignored = _case("gain_not_silently_ignored")
DeviceControlTests.test_channel_atomic_callback = _case("channel_atomic_callback")
DeviceControlTests.test_acquisition_fault_drains_then_errors = _case("acquisition_fault_drains_then_errors")
DeviceControlTests.test_invalid_channel_request = _case("invalid_channel_request")
# Avoid unittest rediscovering the imported base class.
del BCILifetimeTest
if __name__ == "__main__":
    unittest.main()
