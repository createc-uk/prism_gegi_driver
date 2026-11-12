#!/usr/bin/env python3
"""Create an isolated unit-test environment without sudo or system pip."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import urllib.request
import venv

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / '.venv-tests'


def main():
    if ENV.exists() and not (ENV / 'pyvenv.cfg').is_file():
        raise RuntimeError('Refusing to overwrite a non-venv directory: ' + str(ENV))
    # Ubuntu may omit ensurepip; the stdlib can still create a complete venv.
    venv.EnvBuilder(with_pip=False).create(ENV)
    python = str(ENV / 'bin/python')
    if subprocess.run([python, '-m', 'pip', '--version'], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        print('Bootstrapping pip inside .venv-tests (system Python is unchanged)', flush=True)
        with urllib.request.urlopen('https://pypi.org/pypi/pip/json', timeout=60) as response:
            metadata = json.load(response)
        wheel = next(item for item in metadata['urls'] if item['filename'].endswith('.whl'))
        with urllib.request.urlopen(wheel['url'], timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != wheel['digests']['sha256']:
            raise RuntimeError('pip wheel checksum mismatch')
        with tempfile.TemporaryDirectory(prefix='gegi-pip-') as tmp:
            path = Path(tmp) / wheel['filename']
            path.write_bytes(data)
            subprocess.run([python, '-c',
                'import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module("pip",run_name="__main__")',
                str(path), 'install', '--disable-pip-version-check', 'pip'], check=True)
    subprocess.run([python, '-m', 'pip', 'install', '--disable-pip-version-check',
                    '-r', str(ROOT / 'test/requirements.txt')], check=True)
    print('Test environment ready:', ENV)


if __name__ == '__main__':
    main()
