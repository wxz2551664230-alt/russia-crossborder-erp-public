import json
import subprocess
import sys
from pathlib import Path


def test_child_error_keeps_only_stage_and_exception_type_locally(tmp_path):
    source = tmp_path / 'input.json'
    source.write_text('{"unrelated_secret":"must-not-leak"}', encoding='utf-8')
    worker = Path(__file__).resolve().parents[1] / 'client' / 'desktop' / 'order_worker.py'
    subprocess.run([sys.executable, str(worker), '--child', str(source)],
                   check=True, timeout=15)
    result = json.loads((tmp_path / 'transport-result.json').read_text(encoding='utf-8'))
    assert result['code'] == 'local_execution_failed'
    assert result['diagnostic'] == {'phase': 'load_input', 'exception_type': 'KeyError'}
    assert 'must-not-leak' not in json.dumps(result)
