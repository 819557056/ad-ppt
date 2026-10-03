"""The editor consumes a generated type artifact from the server Scene schema."""
from pathlib import Path
import subprocess
import sys


def test_normalized_scene_types_are_generated_from_current_schema():
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run([sys.executable, str(root / 'scripts' / 'generate_scene_types.py'), '--check'],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout
