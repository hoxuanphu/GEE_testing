"""Build a Windows release ZIP from approved sources and verified local wheels."""
import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import zipfile

from tools.start_workspace import LOCK, ROOT, SetupError, check_boundary_files


LEAFLET = 'static/vendor/leaflet'
LEAFLET_FILES = (
    'LICENSE', 'leaflet.css', 'leaflet.js', 'leaflet.js.map',
    *(f'images/{name}.png' for name in ('layers-2x', 'layers', 'marker-icon-2x', 'marker-icon', 'marker-shadow')),
)
TASK_CENTER_FILES = (
    'app/cog_writer.py', 'app/task_center.py', 'app/task_files.py', 'app/task_lock.py', 'static/task-center.js',
)


def source_names(root):
    names = subprocess.run(
        ['git', 'ls-files', '-z', '--', 'app', 'static', 'shapefile34', 'Shapefile_PX_VN_34'],
        cwd=root, check=True, capture_output=True,
    ).stdout.decode('utf-8').split('\0')
    return sorted({'Start.cmd', 'tools/start_workspace.py', LOCK, 'README.md',
                   'LICH_SU_THAY_DOI.md', 'static/provider-warning.js',
                   *(name for name in names if name),
                   *TASK_CENTER_FILES,
                   *(f'{LEAFLET}/{file}' for file in LEAFLET_FILES)})


def package(root, wheelhouse, output):
    check_boundary_files(root)
    names = source_names(root)
    wheels = sorted(wheelhouse.glob('*.whl'))
    expected = set(re.findall(r'--hash=sha256:([a-f0-9]{64})', (root / LOCK).read_text(encoding='utf-8')))
    actual = set()
    for wheel in wheels:
        with wheel.open('rb') as stream:
            actual.add(hashlib.file_digest(stream, 'sha256').hexdigest())
    if not expected or len(wheels) != len(expected) or actual != expected:
        raise SetupError('Wheelhouse phải chứa đúng các wheel được ghi trong tệp khóa.')

    for name in names:
        path = root / name
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise SetupError(f'Thiếu tệp trong gói {name}')
        with path.open('rb') as stream:
            if stream.read(43).startswith(b'version https://git-lfs.github.com/spec/v1'):
                raise SetupError(f'Chưa tải nội dung Git LFS của {name}')

    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=5) as archive:
        for name in names:
            archive.write(root / name, name)
        for wheel in wheels:
            archive.write(wheel, f'wheelhouse/{wheel.name}', compress_type=zipfile.ZIP_STORED)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheelhouse', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(package(ROOT, args.wheelhouse, args.output))


if __name__ == '__main__':
    main()
