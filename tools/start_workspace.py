"""Set up and start Satellite Workspace on Windows using standard Python."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import sysconfig
import threading
import time
import urllib.request
import venv
import webbrowser


ROOT = Path(__file__).resolve().parents[1]
HOST = '127.0.0.1'
LOCK = 'requirements-windows-py312.lock'
COMMUNE_STEM = 'Shapefile_PX_VN_34/Việt Nam (phường xã) - 34'
PROVINCE_STEM = 'shapefile34/Việt Nam (tỉnh thành) - 34'
DATA_FILES = ('shapefile34/vn34.json', *(stem + ext for stem in (PROVINCE_STEM, COMMUNE_STEM)
                                       for ext in ('.shp', '.dbf', '.shx', '.prj', '.cpg')))
PYTHON_REQUIREMENT = 'Cần Python 3.12 x64 trên Windows từ nguồn được IT phê duyệt.'
PYTHON_CHECK = "import sys, sysconfig; sys.exit(not (sys.version_info[:2] == (3, 12) and sysconfig.get_platform() == 'win-amd64'))"


class SetupError(RuntimeError):
    pass


def check_boundary_files(root):
    for name in DATA_FILES:
        try:
            with (root / name).open('rb') as stream:
                initial = stream.read(64)
        except FileNotFoundError:
            initial = b''
        if not initial or initial.startswith(b'version https://git-lfs.github.com/spec/v1'):
            raise SetupError(f'Thiếu dữ liệu {name}. Dùng ZIP phát hành đầy đủ hoặc chạy git lfs pull trong bản clone Git LFS.')


def run_checked(args, root, message):
    try:
        subprocess.run(args, cwd=root, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SetupError(message) from exc


@contextmanager
def installation_lock(root):
    import msvcrt

    with (root / '.workspace-install.lock').open('a+b') as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise SetupError('Một cửa sổ Satellite Workspace đang chuẩn bị môi trường. Hãy chờ cửa sổ đó.') from exc
        yield


def prepare_environment(root):
    lock = root / LOCK
    wheelhouse = root / 'wheelhouse'
    if not lock.is_file() or not wheelhouse.is_dir():
        raise SetupError('Thiếu tệp khóa hoặc wheelhouse. Hãy giải nén đầy đủ gói ZIP Windows.')
    expected = hashlib.sha256(lock.read_bytes()).hexdigest()
    env = root / '.venv'
    python = env / 'Scripts' / 'python.exe'
    stamp = env / '.satellite-workspace.sha256'
    if python.is_file() and stamp.is_file() and stamp.read_text(encoding='ascii').strip() == expected:
        return python

    stamp.unlink(missing_ok=True)
    if not python.is_file():
        print('Đang tạo môi trường .venv…', flush=True)
        venv.EnvBuilder(with_pip=True).create(env)
    run_checked([python, '-I', '-c', PYTHON_CHECK], root, PYTHON_REQUIREMENT)
    install = [python, '-I', '-m', 'pip', '--disable-pip-version-check', 'install',
               '--require-hashes', '--only-binary=:all:', '--no-index', '--no-cache-dir',
               '--find-links', wheelhouse, '-r', lock]
    print('Đang cài thư viện từ wheelhouse cục bộ…', flush=True)
    run_checked(install, root, 'Cài thư viện thất bại. Xem lỗi pip ở trên và kiểm tra gói ZIP Windows.')
    run_checked([python, '-I', '-m', 'pip', 'check'], root, 'Các phiên bản thư viện trong .venv chưa tương thích.')
    stamp.write_text(expected + '\n', encoding='ascii')
    return python


def open_when_ready(url, finished):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 120
    while not finished.is_set() and time.monotonic() < deadline:
        try:
            with opener.open(url + '/api/health', timeout=1) as response:
                ready = json.load(response).get('status') == 'ok'
        except (OSError, ValueError):
            ready = False
        if ready:
            if not finished.is_set():
                webbrowser.open(url)
            return
        finished.wait(.25)
    if not finished.is_set():
        print(f'Máy chủ phản hồi chậm. Kiểm tra nhật ký ở cửa sổ này và mở {url}.', flush=True)


def serve(port, no_browser):
    import uvicorn

    url = f'http://{HOST}:{port}'
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind((HOST, port))
        except OSError as exc:
            raise SetupError(f'Không mở được cổng {port}. Kiểm tra cửa sổ đang chạy hoặc chọn --port khác.') from exc
        finished = threading.Event()
        print(f'Satellite Workspace · {url} · Ctrl+C để dừng', flush=True)
        if not no_browser:
            threading.Thread(target=open_when_ready, args=(url, finished), daemon=True).start()
        server = uvicorn.Server(uvicorn.Config('app.main:app', host=HOST, port=port))
        try:
            server.run(sockets=[listener])
        finally:
            finished.set()
    return 0 if server.started else 1


def main():
    parser = argparse.ArgumentParser(description='Khởi chạy Satellite Workspace cục bộ')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if sysconfig.get_platform() != 'win-amd64' or sys.version_info[:2] != (3, 12):
            raise SetupError(PYTHON_REQUIREMENT)
        if not 1 <= args.port <= 65535:
            raise SetupError('Cổng phải nằm trong khoảng 1 đến 65535.')
        if args.serve:
            return serve(args.port, args.no_browser)
        check_boundary_files(ROOT)
        with installation_lock(ROOT):
            python = prepare_environment(ROOT)
        command = [python, '-E', '-s', '-m', 'tools.start_workspace', '--serve', '--port', str(args.port)]
        if args.no_browser:
            command.append('--no-browser')
        return subprocess.call(command, cwd=ROOT)
    except KeyboardInterrupt:
        return 0
    except (SetupError, OSError, subprocess.SubprocessError) as exc:
        print(f'Không khởi chạy được Satellite Workspace. {exc}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    sys.exit(main())
