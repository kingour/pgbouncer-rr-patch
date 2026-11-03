"""Exercise the Makefile patch with modern, legacy and broken config tools."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class PythonConfigTests(unittest.TestCase):
    def run_config(self, mode):
        patch = (Path(__file__).resolve().parents[1] / "Makefile.diff").read_text()
        block = patch.split('+PYTHON_CONFIG ?=', 1)[1].split('+pgbouncer_CPPFLAGS', 1)[0]
        block = 'PYTHON_CONFIG ?=' + block.replace('\n+', '\n')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'python-config'
            config.write_text('''#!/bin/sh
case "$1" in
--includes)
    [ "$MODE" != includes-fail ] || exit 1
    echo -I/test/include ;;
--embed)
    [ "$MODE" = modern ] || exit 1
    echo '-L/test/lib -lpython3.11 -framework CoreFoundation' ;;
--ldflags)
    [ "$MODE" = legacy ] || exit 1
    echo '-L/test/lib -lpython2.7' ;;
*) exit 1 ;;
esac
''')
            config.chmod(0o755)
            makefile = root / 'Makefile'
            makefile.write_text(block + '\nall:\n\t@echo "$(python_CPPFLAGS) $(python_LDFLAGS)"\n')
            return subprocess.run(
                ['make', '-f', str(makefile), 'PYTHON_CONFIG=' + str(config)],
                env=dict(os.environ, MODE=mode), capture_output=True, text=True,
            )

    def test_modern_embedding_and_framework(self):
        result = self.run_config('modern')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('-lpython3.11 -Wl,-framework,CoreFoundation', result.stdout)

    def test_legacy_fallback(self):
        result = self.run_config('legacy')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('-I/test/include -L/test/lib -lpython2.7', result.stdout)

    def test_include_failure_stops_make(self):
        result = self.run_config('includes-fail')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot get Python includes', result.stderr)

    def test_link_flag_failure_stops_make(self):
        result = self.run_config('broken')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot get Python embedding flags', result.stderr)


if __name__ == '__main__':
    unittest.main()
