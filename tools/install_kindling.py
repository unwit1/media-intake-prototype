"""Fetch the pinned official Kindling Windows release used by the portable build."""
import hashlib
from pathlib import Path
from urllib.request import Request, urlopen


VERSION = 'v0.45.6'
SHA256 = '1988a319d0d9af2e75cf776eb9e63a59412fcd8ebfc5d0cebb000519ccd99f54'
URL = (f'https://github.com/ciscoriordan/kindling/releases/download/{VERSION}/'
       'kindling-cli-windows.exe')
TARGET = Path(__file__).resolve().parent / 'kindling-cli.exe'
TEMPORARY = TARGET.with_suffix('.download')


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def install():
    if TARGET.is_file() and file_sha256(TARGET) == SHA256:
        print(f'Kindling {VERSION} is already installed.')
        return
    request = Request(URL, headers={'User-Agent': 'MediaIntake portable installer'})
    try:
        with urlopen(request, timeout=120) as response, TEMPORARY.open('wb') as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        actual = file_sha256(TEMPORARY)
        if actual != SHA256:
            raise RuntimeError('Kindling release SHA-256 did not match the pinned GitHub digest.')
        TEMPORARY.replace(TARGET)
        print(f'Installed Kindling {VERSION}; SHA-256 verified.')
    finally:
        TEMPORARY.unlink(missing_ok=True)


if __name__ == '__main__':
    install()
