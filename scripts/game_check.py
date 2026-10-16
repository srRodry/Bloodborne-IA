#!/usr/bin/env python3
"""The game files bbport runs: Bloodborne with the 1.09 update merged in. The retail releases (the
US CUSA00900, the European Game of the Year edition CUSA03173, the Asian The Old Hunters Edition
CUSA03023, ...) share the same 1.09 executable.

Other versions start and then fail inside the game's code (the base game 1.00 faults at guest
offset 0x20348b8): hooks and patches use the addresses of this one executable. The check compares
the loaded executable image, whose hash is the same whatever tool dumped it (the SELF headers
around it differ). The shaders must also unpack: an extraction tool that leaves
parts of compressed sectors stale (LibOrbisPkg PkgTool on .NET 6 or newer, issue #81) makes the
game hang while it loads them, with no error. BB_SKIP_GAME_CHECK=1 skips both.

Run as a script, it checks every .dcx file of a game folder: game_check.py GAME_DIR
"""
import hashlib
import os
import struct
import sys
import zlib
from pathlib import Path

# The retail releases (the list of content IDs from PR #117): Bloodborne, then the editions with
# The Old Hunters. The executable decides in the end: SUPPORTED_IMAGE.
SUPPORTED_TITLES = ('CUSA00900', 'CUSA00207', 'CUSA00208', 'CUSA00299', 'CUSA01363',  # US EU UK JP AS
                    'CUSA03179', 'CUSA03173', 'CUSA03014', 'CUSA03023')  # US EU JP AS
SUPPORTED_VERSION = '01.09'
OLD_HUNTERS_TITLES = {'CUSA03179', 'CUSA03173', 'CUSA03014', 'CUSA03023'}
OLD_HUNTERS_AREAS = ('m34_00_00_00', 'm35_00_00_00', 'm36_00_00_00')
OLD_HUNTERS_TITLE_PACK = 'dvdroot_ps4/menu/title.tpf.dcx'
OLD_HUNTERS_TITLE_ART = 'MENU_Title_00004'
SUPPORTED_IMAGE = '071df19c8880086d97182dbc057bc8cb37badaca57d9112683836b24a0444c0a'
# The 1.09 executable with Lance McDonald's 60 fps patch applied to it, as some dumps ship it
# (seen on US CUSA00900): one of its edits inverts a branch in the menu code (+0xf6d90d) and the
# gestures menu crashes the game. bbport has its own 60 fps; the clean 1.09 eboot.bin is needed.
LANCE_60FPS_IMAGE = 'bbc91f4dff6bc3118b039464289e39878a8fdfc802a07fab9d5fe84d935a676f'
# The game loads these first; a broken extraction damages most files, these among them.
CHECKED_FOLDER = 'dvdroot_ps4/shader'
# A known 1.09 world-layout marker. A mixed base/update folder can have the correct 1.09 eboot and
# param.sfo yet retain older data files; Dantelion2 then panics in FileTransferTask.cpp(865) and
# bbport faults at guest offset 0x20b56d2 when entering the world. The decompressed Hunter's Dream
# layout is stable across the supported retail 1.09 releases.
CHECKED_LAYOUT = 'dvdroot_ps4/map/mapstudio/m21_00_00_00.msb.dcx'
LAYOUT_109_SHA256 = '8e1de0ee223cd5137a93383cfc7bb1d27964bc480c63e35013674717b403ef8f'


def image_sha256(game):
    """The SHA-256 of eboot.bin's loaded image (its loadable segments at their addresses)."""
    from prepare import parse_self, span  # same directory
    elf, header, ph, _segments, _missing = parse_self((Path(game) / 'eboot.bin').read_bytes())
    loads = [p for p in ph if p['type'] in (1, 0x61000010)]
    image = bytearray(max(p['vaddr'] + p['memsz'] for p in loads))
    for p in loads:
        image[p['vaddr']:p['vaddr'] + p['filesz']] = span(elf, p['offset'], p['filesz'])
    return hashlib.sha256(image).hexdigest()


def dcx_broken(path):
    """True when a DFLT .dcx file does not unpack to its stated size or fails zlib's checksum.
    Other compressions are not checked."""
    data = Path(path).read_bytes()
    if data[:4] != b'DCX\0' or data[0x18:0x1C] != b'DCS\0' or data[0x28:0x2C] != b'DFLT':
        return False
    size, packed = struct.unpack_from('>II', data, 0x1C)
    try:
        return len(zlib.decompress(data[0x4C:0x4C + packed])) != size
    except zlib.error:
        return True


def broken_files(game, folder=CHECKED_FOLDER):
    """The .dcx files under the game's folder that do not unpack (paths relative to the game)."""
    root = Path(game)
    return [str(f.relative_to(root)) for f in sorted((root / folder).rglob('*.dcx')) if dcx_broken(f)]


def dflt_payload(path):
    """The decompressed payload of a DFLT DCX, None for another compression; ValueError if damaged."""
    data = Path(path).read_bytes()
    if data[:4] != b'DCX\0' or data[0x18:0x1C] != b'DCS\0' or data[0x28:0x2C] != b'DFLT':
        return None
    if len(data) < 0x4C:
        raise ValueError('short DCX header')
    size, packed = struct.unpack_from('>II', data, 0x1C)
    if 0x4C + packed > len(data):
        raise ValueError('truncated DCX payload')
    try:
        payload = zlib.decompress(data[0x4C:0x4C + packed])
    except zlib.error as error:
        raise ValueError(f'DCX zlib error: {error}') from error
    if len(payload) != size:
        raise ValueError(f'DCX size mismatch: {len(payload)} != {size}')
    return payload


def stale_update_data(game):
    """True when a known 1.09 world layout is present but is not the 1.09 version.

    Absence is not treated as proof here: some test/minimal layouts do not carry the world data.
    A real mixed base/update dump normally has this file, but with the older payload.
    """
    path = Path(game) / CHECKED_LAYOUT
    if not path.is_file():
        return False
    try:
        payload = dflt_payload(path)
    except (OSError, ValueError):
        return True
    if payload is None:
        return False
    return hashlib.sha256(payload).hexdigest() != LAYOUT_109_SHA256



def old_hunters_missing(game):
    """First missing Old Hunters marker for editions that package the DLC, else None.

    The 1.07+ update carries the DLC's title art and three areas. A 1.09 eboot/param.sfo beside
    older base-game data is a known cause of FileTransferTask.cpp(865) / guest fault 0x20b56d2.
    """
    root = Path(game)
    pack = root / OLD_HUNTERS_TITLE_PACK
    if not pack.is_file():
        return OLD_HUNTERS_TITLE_PACK
    try:
        raw = pack.read_bytes()
        payload = dflt_payload(pack)
    except (OSError, ValueError):
        return OLD_HUNTERS_TITLE_PACK
    data = payload if payload is not None else raw
    narrow = OLD_HUNTERS_TITLE_ART.encode('ascii') + b'\0'
    wide = OLD_HUNTERS_TITLE_ART.encode('utf-16-le') + b'\0\0'
    if narrow not in data and wide not in data:
        return f'{OLD_HUNTERS_TITLE_ART} in {OLD_HUNTERS_TITLE_PACK}'
    for area in OLD_HUNTERS_AREAS:
        relative = f'dvdroot_ps4/map/{area}'
        if not (root / relative).is_dir():
            return relative
    return None


def problem(game, image_hash=None):
    """None for the supported game, else (kind, title, version): kind is 'missing_update' (base
    game or an older update), 'wrong_eboot' (param.sfo says 1.09, eboot.bin is another version),
    'patched_eboot' (the 1.09 eboot.bin with a 60 fps patch baked in), 'other_title' (another
    edition), 'unreadable', 'stale_update_data' (1.09 executable with older world data) or
    'damaged_files' (the right game, but its shaders do not unpack: a broken extraction)."""
    if os.environ.get('BB_SKIP_GAME_CHECK') == '1':
        return None
    from prepare import sfo
    try:
        info = sfo((Path(game) / 'sce_sys/param.sfo').read_bytes())
    except (OSError, ValueError, IndexError):
        info = {}
    title, version = info.get('TITLE_ID', '?'), info.get('APP_VER', '?')
    try:
        image_hash = image_hash or image_sha256(game)
        if image_hash == SUPPORTED_IMAGE:
            if stale_update_data(game):
                return 'stale_update_data', title, version
            if title in OLD_HUNTERS_TITLES and old_hunters_missing(game):
                return 'missing_old_hunters_data', title, version
            return ('damaged_files', title, version) if broken_files(game) else None
    except (OSError, ValueError, IndexError, StopIteration, KeyError):
        return 'unreadable', title, version
    if image_hash == LANCE_60FPS_IMAGE:
        return 'patched_eboot', title, version
    if title not in SUPPORTED_TITLES:
        return 'other_title', title, version
    if version != SUPPORTED_VERSION:
        return 'missing_update', title, version
    return 'wrong_eboot', title, version


def explain(kind, title, version):
    """What is wrong and what to do, for the log (English)."""
    found = f'Found {title} version {version} (sce_sys/param.sfo).'
    return {
        'missing_update': f'{found} bbport needs the 1.09 update merged into the game folder: copy '
                          'everything from the dumped 1.09 update (e.g. CUSA03173-patch) into the '
                          'game folder, replacing files (eboot.bin and sce_sys too).',
        'wrong_eboot': f'{found} param.sfo is from 1.09 but eboot.bin is not: copy eboot.bin from '
                       'the dumped 1.09 update into the game folder, replacing the old one.',
        'patched_eboot': f'{found} This eboot.bin has the 60 fps patch by Lance McDonald applied '
                         'to it, which crashes the game when the gestures menu opens (bbport has its '
                         'own 60 fps): copy eboot.bin from a clean dump of the 1.09 update into the '
                         'game folder, replacing this one.',
        'other_title': f'{found} This is not a retail release of Bloodborne: bbport runs Bloodborne '
                       '(any region, The Old Hunters editions too) with update 1.09.',
        'unreadable': f'{found} eboot.bin could not be read as a decrypted PS4 executable: dump '
                      'the game and the 1.09 update again.',
        'stale_update_data': f'{found} The executable is 1.09, but {CHECKED_LAYOUT} is not the '
                             '1.09 world layout. This mixed base/update state is known to crash in '
                             'Dantelion2 FileTransferTask.cpp(865), guest offset 0x20b56d2, while '
                             'entering the world. Copy every file from the 1.09 update over the base '
                             'game again and REPLACE existing files; do not choose Skip. If files '
                             'were modified directly by a mod, restore the clean 1.09 game data and '
                             'use the app\'s separate Mods folder instead.',
        'missing_old_hunters_data': f'{found} This Old Hunters edition is missing data that ships '
                                    f'with the 1.09 update (title art or m34/m35/m36 areas). This '
                                    f'incomplete base/update set is known to fail in '
                                    f'FileTransferTask.cpp(865), guest offset 0x20b56d2. Reapply '
                                    f'the complete 1.09 update with Replace, or keep a complete '
                                    f'{title}-UPDATE / {title}-patch folder beside the base game.',
        'damaged_files': f'{found} The game files are damaged: the shaders in {CHECKED_FOLDER} do '
                         'not unpack, and the game would hang while loading them. The extraction '
                         'tool left parts of compressed sectors stale (LibOrbisPkg PkgTool on '
                         '.NET 6 or newer, issue #81): extract the game and the 1.09 update again '
                         'with a fixed tool. scripts/game_check.py GAME_DIR checks every file.',
    }[kind]


def main(argv):
    """game_check.py GAME_DIR: every .dcx file of the game, those that do not unpack listed."""
    if len(argv) != 2:
        print('Usage: game_check.py GAME_DIR', file=sys.stderr)
        return 2
    game = Path(argv[1])
    files = sorted((game / 'dvdroot_ps4').rglob('*.dcx'))
    broken = []
    for number, path in enumerate(files, 1):
        if dcx_broken(path):
            broken.append(path)
            print(f'damaged: {path.relative_to(game)}')
        if number % 1000 == 0:
            print(f'{number} of {len(files)} checked', file=sys.stderr)
    print(f'{len(broken)} of {len(files)} .dcx files do not unpack')
    return 1 if broken else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
