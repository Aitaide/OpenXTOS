"""One-shot software-only demo; never initializes a production authority."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import time
from xtos_action_gate import ActionGate, FixtureStore
from . import MultimodalSession


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true', required=True)
    parser.parse_args(argv)
    # /tmp is world writable and correctly refused by FixtureStore's ancestor
    # validation. HOME is trusted launch configuration, not a sample field.
    with tempfile.TemporaryDirectory(prefix='.xtos-xr-demo-', dir=Path.home()) as owned:
        gate = ActionGate(FixtureStore(Path(owned) / 'authority'))
        # Explicit simulator seam, not a runtime or calibrated device clock.
        def simulated_convert(domain,timestamp):
            if domain!='openxr_time':raise ValueError('unsupported simulated clock')
            return dict(converted_host_ns=timestamp,uncertainty_ns=0,
                        conversion_id='simulated-per-point')
        session = MultimodalSession(clock=time.monotonic_ns,gate=gate,converter=simulated_convert,
            max_age_ns=60000000000, candidate_ttl_ns=60000000000)
        try:
            now = time.monotonic_ns()
            for modality in ('eeg', 'eye_gaze', 'emg'):
                session.ingest(dict(schema='xtos.multimodal.sample.v1', modality=modality,
                    source_id=modality+'-simulation', source_kind='simulated_runtime',
                    clock_domain='openxr_time' if modality=='eye_gaze' else 'host_monotonic_ns',source_timestamp_ns=now,
                    host_observed_ns=now, sequence=1, generation=1,
                    quality=dict(valid=True, tracked=True, predicted=False), values=[0.0]))
            candidate = session.join()
            nonce = gate.approve(session.approval_request(candidate),
                                 actor='owned-demo-fixture', ttl_seconds=60)
            receipt = session.confirm(candidate, nonce)
            print(json.dumps(asdict(receipt), sort_keys=True, allow_nan=False))
        finally:
            session.close()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
