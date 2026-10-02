"""Validate downloaded comic images before they are archived.

``inspect_image`` checks the file signature, reads width and height, and
requires the format's end marker, so truncated or corrupt transfers are
rejected rather than archived. PNG, JPEG, GIF and WebP are understood
natively; when Pillow is installed it also fully decodes the image.
"""
import io
import struct


class CorruptImage(ValueError):
    """The bytes are not a complete, valid image (often a truncated transfer)."""


def _png(data):
    if len(data) < 33 or data[12:16] != b'IHDR':
        raise CorruptImage('PNG header is incomplete')
    width, height = struct.unpack('>II', data[16:24])
    position, seen_end = 8, False
    while position + 8 <= len(data):
        length = struct.unpack('>I', data[position:position + 4])[0]
        kind = data[position + 4:position + 8]
        position += 12 + length
        if position > len(data):
            raise CorruptImage('PNG is truncated')
        if kind == b'IEND':
            seen_end = True
            break
    if not seen_end:
        raise CorruptImage('PNG is truncated (no IEND chunk)')
    return '.png', width, height


def _jpeg(data):
    position, size = 2, None
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            raise CorruptImage('JPEG segment structure is broken')
        marker = data[position + 1]
        if marker == 0xFF:
            position += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            position += 2
            continue
        length = struct.unpack('>H', data[position + 2:position + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC) and position + 9 <= len(data):
            height, width = struct.unpack('>HH', data[position + 5:position + 9])
            size = (width, height)
        if marker == 0xDA:  # start of scan: compressed data follows until EOI
            break
        position += 2 + length
    if size is None:
        raise CorruptImage('JPEG has no frame header')
    if b'\xff\xd9' not in data[-4096:]:
        raise CorruptImage('JPEG is truncated (no end-of-image marker)')
    return '.jpg', size[0], size[1]


def _gif(data):
    if len(data) < 14:
        raise CorruptImage('GIF header is incomplete')
    width, height = struct.unpack('<HH', data[6:10])
    if b'\x3b' not in data[-16:]:
        raise CorruptImage('GIF is truncated (no trailer)')
    return '.gif', width, height


def _webp(data):
    if len(data) < 30:
        raise CorruptImage('WebP header is incomplete')
    declared = struct.unpack('<I', data[4:8])[0] + 8
    if declared > len(data):
        raise CorruptImage('WebP is truncated')
    chunk = data[12:16]
    if chunk == b'VP8 ':
        width, height = (struct.unpack('<HH', data[26:30]))
        return '.webp', width & 0x3FFF, height & 0x3FFF
    if chunk == b'VP8L':
        bits = struct.unpack('<I', data[21:25])[0]
        return '.webp', (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b'VP8X':
        width = int.from_bytes(data[24:27], 'little') + 1
        height = int.from_bytes(data[27:30], 'little') + 1
        return '.webp', width, height
    raise CorruptImage('Unknown WebP encoding')


def _decode_with_pillow(data):
    try:
        from PIL import Image
    except ImportError:
        return
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except Exception as exc:
        raise CorruptImage(f'Image does not decode: {exc}') from exc


def inspect_image(data, media_type=''):
    """Return (extension, width, height); raise CorruptImage for anything incomplete or unsupported."""
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        result = _png(data)
    elif data.startswith(b'\xff\xd8\xff'):
        result = _jpeg(data)
    elif data.startswith((b'GIF87a', b'GIF89a')):
        result = _gif(data)
    elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        result = _webp(data)
    else:
        raise CorruptImage('Not a PNG, JPEG, GIF or WebP image')
    media_type = str(media_type or '').split(';', 1)[0].strip().casefold()
    if media_type and not media_type.startswith('image/') and media_type != 'application/octet-stream':
        raise CorruptImage(f'Server labelled the image as {media_type}')
    if not (0 < result[1] <= 65535 and 0 < result[2] <= 65535):
        raise CorruptImage('Image dimensions are invalid')
    _decode_with_pillow(data)
    return result
