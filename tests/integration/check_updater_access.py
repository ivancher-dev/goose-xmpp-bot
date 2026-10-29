"""Проверка Linux DAC от другого UID в одноразовом контейнере без сети.

Нужны root внутри контейнера, исходники /repo и site-packages установленного
wheel в /installed-packages (только код, без семейных данных). Создаётся
изолированная установка с вымышленной конфигурацией; XMPP/API не используются.
"""

import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile


def main():
    assert os.geteuid() == 0, 'Запускать только в одноразовом root-контейнере'
    module = runpy.run_path('/repo/src/goose/maintenance.py', run_name='access_check')
    with tempfile.TemporaryDirectory(prefix='goose-access-') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        prefix = root / 'app'
        prefix.mkdir(mode=0o755)
        previous = os.umask(0o077)
        try:
            releases = prefix / 'releases'
            releases.mkdir(mode=0o755)
            candidate = releases / 'candidate'
            candidate.mkdir(mode=0o755)
            venv = candidate / 'venv'
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', venv], check=True)
            shutil.copytree('/installed-packages', venv / 'lib/python3.12/site-packages', dirs_exist_ok=True)
            # Старый код правил дочерние файлы, пропуская два родительских каталога.
            for path in candidate.rglob('*'):
                if not path.is_symlink():
                    mode = path.stat().st_mode
                    path.chmod(mode | (0o555 if path.is_dir() or mode & 0o111 else 0o444))
        finally:
            os.umask(previous)
        data = root / 'data'
        data.mkdir(mode=0o700)
        os.chown(data, 65534, 65534)
        config = root / 'config.toml'
        config.write_text(Path('/repo/config/config.example.toml').read_text().replace(
            '/var/lib/goose-xmpp-bot', str(data)))
        config.chmod(0o644)

        def drop_identity():
            os.setgroups([])
            os.setgid(65534)
            os.setuid(65534)

        def doctor():
            try:
                return subprocess.run([venv / 'bin/python', '-m', 'goose.cli', 'doctor', '--config', config],
                                      cwd=prefix, preexec_fn=drop_identity, capture_output=True).returncode
            except PermissionError:
                return 126

        assert doctor() != 0, 'Старая закрытая цепочка должна воспроизводить отказ'
        # Настоящий stage исправляет общий releases до сборки; сеть подставлена.
        updater = object.__new__(module['Updater'])
        updater.paths = {'prefix': prefix, 'repo': root / 'repo'}
        def runner(args, **kwargs):
            if 'archive' in args:
                raise module['UpdateError']('Остановка до архива')
            return 'a' * 40 if '--verify' in args else ''
        updater.run = runner
        try:
            updater.stage('main')
        except module['UpdateError']:
            pass
        module['readable_release'](candidate)
        assert doctor() == 0, 'Исправленное окружение должно пройти doctor под UID 65534'
        assert releases.stat().st_mode & 0o777 == 0o755
        assert candidate.stat().st_mode & 0o777 == 0o755
        assert data.stat().st_mode & 0o777 == 0o700 and not list(data.iterdir())
        print('PASS: отказ до исправления; installed-wheel doctor от UID 65534 успешен после исправления; данные 0700, БД не создана')


if __name__ == '__main__':
    main()
