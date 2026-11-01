"""Local release scratch must respect the caller's workspace temp root."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('temp_root', ['D:/release scratch', 'D:/release-scratch'])
def test_release_archive_temp_root_respects_tmpdir(temp_root):
    bash = shutil.which('bash')
    if bash is None:
        pytest.skip('bash is required to exercise the deployment shell script')
    script = Path(__file__).resolve().parents[1].joinpath('deploy.sh').read_text(encoding='utf-8')
    assignment = next(line for line in script.splitlines() if line.startswith('ARTIFACT_DIR='))
    # Intercept allocation: the regression proves actual shell argument handling
    # without creating a directory in the old, unwanted system temp location.
    probe = ('printf "expected:%s\\n" "$TMPDIR/mathagent-release.XXXXXX";\n'
             + 'mktemp() { printf "%s\\n" "$@"; };\n' + assignment
             + '\nprintf "%s\\n" "$ARTIFACT_DIR"')
    result = subprocess.run([bash, '-c', probe], env={**os.environ, 'TMPDIR': temp_root},
                            capture_output=True, text=True, check=True)
    lines = result.stdout.splitlines()
    # MSYS normalizes a native TMPDIR to /d/... on entry; compare in that
    # shell's path space, while still verifying the whole argument with spaces.
    assert lines == [lines[0], '-d', lines[0].removeprefix('expected:')]
