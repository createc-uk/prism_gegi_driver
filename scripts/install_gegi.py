#!/usr/bin/env python3
"""Install or uninstall the repo-managed GeGi CLI and shortcuts."""
import argparse
import importlib.machinery
import importlib.util
from pathlib import Path
import re
import shlex
import sys

SOURCE = Path(__file__).resolve().with_name('gegi')
loader = importlib.machinery.SourceFileLoader('gegi_cli', str(SOURCE))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)
MARKER = 'GeGi command shortcuts (managed by install_gegi.py).'
BEGIN = '# >>> GeGi CLI (managed) >>>'
END = '# <<< GeGi CLI (managed) <<<'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=Path.home() / '.local')
    parser.add_argument('--mode', choices=['docker', 'native'], default='docker')
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--no-shell', action='store_true', help='Do not update Bash PATH')
    parser.add_argument('--shell-rc', type=Path, default=Path.home() / '.bashrc')
    opt = parser.parse_args()
    bindir = opt.prefix.expanduser().resolve() / 'bin'
    target = bindir / 'gegi'
    aliases = [bindir / name for name in cli.ALIASES]
    # Never overwrite another program or remove unowned files.
    if target.exists() or target.is_symlink():
        if target.is_symlink() or MARKER not in target.read_text():
            raise RuntimeError('Refusing to overwrite/remove ' + str(target))
    for link in aliases:
        if link.is_symlink() and link.readlink() == Path('gegi'):
            continue
        if link.exists() or link.is_symlink():
            raise RuntimeError('Conflicting command: ' + str(link))
    if not opt.uninstall:
        bindir.mkdir(parents=True, exist_ok=True)
        text = SOURCE.read_text()
        text = text.replace('DEFAULT_REPO = str(Path(__file__).resolve().parents[1])',
                            'DEFAULT_REPO = ' + repr(str(SOURCE.parent.parent)))
        text = text.replace("DEFAULT_MODE = 'docker'", 'DEFAULT_MODE = ' + repr(opt.mode))
        target.write_text(text)
        target.chmod(0o755)
        for link in aliases:
            if not link.is_symlink():
                link.symlink_to('gegi')
    else:
        for link in aliases:
            if link.is_symlink():
                link.unlink()
        if target.exists():
            target.unlink()
    if not opt.no_shell:
        rc = opt.shell_rc.expanduser()
        text = rc.read_text() if rc.exists() else ''
        text = re.sub(r'\n?' + re.escape(BEGIN) + r'\n.*?\n' + re.escape(END) + r'\n?', '\n', text, flags=re.S)
        if not opt.uninstall:
            text += '\n' + BEGIN + '\nexport PATH=' + shlex.quote(str(bindir)) + ':"$PATH"\n' + END + '\n'
        rc.write_text(text)
    print(('Removed from ' if opt.uninstall else 'Installed in ') + str(bindir))
    if not opt.no_shell:
        print('Reload: source ' + shlex.quote(str(opt.shell_rc)))

if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError) as error:
        sys.exit(str(error))
