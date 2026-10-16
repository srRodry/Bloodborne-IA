#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Android front for the loose-file mod loader (scripts/mods.py): list/validate mods for the app,
apply them as a symlink overlay of the game folder for run-thor.sh.

Only game version 1.09 data mods are supported: a mod that would put anything outside dvdroot_ps4
(eboot.bin, sce_module/, sce_sys/, param.sfo, executables) is invalid and never applied.

  list     --mods-dir D --config C           one JSON object on stdout (the app parses it)
  validate --mods-dir D --name N             the JSON object of one mod
  apply    --game G --out O --mods-dir D --config C
                                             prints the game path to run (overlay or G itself)
  install-pkg  --pkg P --name N --mods-dir D [--game G] [--tool T]
                                             unpack a PS4 fake PKG with ps4-pkg-tool and keep its
                                             dvdroot_ps4 as the mod N (1.09 data mods only)
  install-game --pkg P --dest DIR [--replace] [--tool T]
                                             unpack a game PKG (base game 'gd' or update 'gp') into
                                             DIR/<TITLE_ID> (DIR may be that folder itself)

install-pkg and install-game print `progress <bytesDone> <bytesTotal> <label>` lines (total 0:
unknown), everything else goes to stderr, and the last stdout line is one JSON object. They always
exit 0.
"""
import argparse
import collections
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys

HERE = Path(__file__).resolve().parent
NOT_A_MOD = 'Not a Bloodborne mod: no dvdroot_ps4 or game folders inside'
# Entries whose presence anywhere in a mod makes it unsupported (compared case-insensitively).
SYSTEM_NAMES = {'eboot.bin', 'param.sfo', 'sce_module', 'sce_sys'}
# Top-level files (outside dvdroot_ps4) that are PS4 executables/modules.
EXECUTABLE_SUFFIXES = ('.self', '.elf', '.oelf', '.prx', '.sprx')


def load_mods():
    """scripts/mods.py: beside this file in the runtime bundle (bbport/scripts/), else the repo's."""
    for candidate in (HERE / 'mods.py', HERE.parent.parent / 'scripts' / 'mods.py'):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location('bbmods', candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise FileNotFoundError('scripts/mods.py not found')


mods = load_mods()


# Android: the app can see /storage/emulated/0 but not always /storage/emulated (lstat: ENOENT),
# so a strict realpath of a shared-storage path fails although the path exists (mods.py resolves
# the game and mod folders strictly). Strict here means: the path exists.
_realpath = os.path.realpath


def _shared_storage_realpath(path, *, strict=False):
    try:
        return _realpath(path, strict=strict)
    except FileNotFoundError:
        if strict and os.path.exists(path):
            return _realpath(path)
        raise


os.path.realpath = _shared_storage_realpath


def load_game_check():
    """scripts/game_check.py (it imports prepare.py from its own folder)."""
    for candidate in (HERE / 'game_check.py', HERE.parent.parent / 'scripts' / 'game_check.py'):
        if candidate.is_file():
            if str(candidate.parent) not in sys.path:
                sys.path.insert(0, str(candidate.parent))
            spec = importlib.util.spec_from_file_location('bbgamecheck', candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise FileNotFoundError('scripts/game_check.py not found')


def read_config(config):
    """(order, disabled) from the desktop-format JSON; ValueError when it is malformed."""
    if not config or not Path(config).is_file():
        return [], set()
    try:
        settings = json.loads(Path(config).read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ValueError(f'mods.json: {error}')
    if not isinstance(settings, dict):
        raise ValueError('mods.json: expected an object')
    order, disabled = settings.get('order', []), settings.get('disabled', [])
    for key, value in (('order', order), ('disabled', disabled)):
        if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
            raise ValueError(f'mods.json: "{key}" must be a list of folder names')
    return order, set(disabled)


def folders(mods_dir):
    """Names of all visible sub-folders of the mods folder."""
    mods_dir = Path(mods_dir)
    if not mods_dir.is_dir():
        return []
    return sorted((p.name for p in mods_dir.iterdir() if p.is_dir() and not p.name.startswith('.')),
                  key=lambda name: (name.casefold(), name))


def load_order(mods_dir, config):
    """Mod names in load order, then the non-mod folders. Same merge as mods.selected():
    configured order first, newly found mods after it alphabetically. -> (mods, others, disabled)"""
    order, disabled = read_config(config)
    # Dot-folders (.staging-* of an install in progress) are never mods.
    available = [n for n in mods.discover(mods_dir) if not n.startswith('.')]
    ordered = list(dict.fromkeys(n for n in [*order, *available] if n in available))
    others = [n for n in folders(mods_dir) if n not in available]
    return ordered, others, disabled


def problem_of(folder):
    """Why a mod folder cannot be used (None when it is a supported data mod)."""
    folder = Path(folder)
    layout = mods.content_root(folder)
    if layout is None:
        return NOT_A_MOD
    root = layout[0].resolve()
    for directory, dirs, files in os.walk(folder, followlinks=False):
        directory = Path(directory)
        for name in [*dirs, *files]:
            if name.casefold() in SYSTEM_NAMES:
                return (f'Not a 1.09 data mod: contains {(directory / name).relative_to(folder).as_posix()} '
                        '(only files inside dvdroot_ps4 are supported)')
            if (directory / name).is_symlink():
                return f'Symlinks are unsupported: {(directory / name).relative_to(folder).as_posix()}'
        if directory.resolve() in (root, folder.resolve()):
            for name in files:
                if name.casefold().endswith(EXECUTABLE_SUFFIXES):
                    return f'Not a 1.09 data mod: contains executable {name} outside dvdroot_ps4'
    return None


def inspect(mods_dir, name, enabled):
    """The mod's row without conflicts, plus its game paths ({casefolded path: size})."""
    row = {'name': name, 'enabled': enabled, 'valid': False, 'problem': None, 'files': 0, 'bytes': 0,
           'overrides': [], 'overriddenBy': []}
    keys = {}
    try:
        row['problem'] = problem_of(Path(mods_dir) / name)
        if row['problem'] is None:
            for relative, source in mods.mod_files(Path(mods_dir) / name):
                keys[relative.as_posix().casefold()] = source.stat().st_size
            row['valid'] = True
            row['files'] = len(keys)
            row['bytes'] = sum(keys.values())
    except (OSError, ValueError) as error:
        row['problem'] = str(error)
    return row, keys


def build_list(mods_dir, config):
    ordered, others, disabled = load_order(mods_dir, config)
    rows, owned = [], {}
    for name in ordered:
        row, keys = inspect(mods_dir, name, name not in disabled)
        rows.append(row)
        owned[name] = keys if row['enabled'] and row['valid'] else {}
    for index, row in enumerate(rows):
        mine = owned[row['name']]
        if not mine:
            continue
        for other in rows[:index]:
            shared = len(mine.keys() & owned[other['name']].keys())
            if shared:
                row['overrides'].append({'mod': other['name'], 'files': shared})
        for other in rows[index + 1:]:
            shared = len(mine.keys() & owned[other['name']].keys())
            if shared:
                row['overriddenBy'].append({'mod': other['name'], 'files': shared})
    for name in others:
        rows.append({'name': name, 'enabled': False, 'valid': False, 'problem': NOT_A_MOD, 'files': 0,
                     'bytes': 0, 'overrides': [], 'overriddenBy': []})
    return rows


def command_list(args):
    result = {'modsDir': str(Path(args.mods_dir).absolute()), 'exists': Path(args.mods_dir).is_dir(),
              'mods': [], 'error': None}
    try:
        result['mods'] = build_list(args.mods_dir, args.config)
    except Exception as error:  # the app shows the message; never a traceback
        result['error'] = f'{type(error).__name__}: {error}'
    print(json.dumps(result, ensure_ascii=False))


def command_validate(args):
    name = args.name
    try:
        if '/' in name or name in ('', '.', '..') or not (Path(args.mods_dir) / name).is_dir():
            raise ValueError(f'No such mod folder: {name}')
        row, _ = inspect(args.mods_dir, name, True)
    except Exception as error:
        row = {'name': name, 'enabled': True, 'valid': False, 'problem': f'{type(error).__name__}: {error}',
               'files': 0, 'bytes': 0, 'overrides': [], 'overriddenBy': []}
    print(json.dumps(row, ensure_ascii=False))


def clear(out, protected):
    """Empty `out` (stale overlays of killed processes) without following links."""
    out = Path(out).resolve()
    for other in protected:
        other = Path(other).resolve()
        if out == other or out in other.parents or other in out.parents:
            raise ValueError(f'Refusing to clear {out}: it overlaps {other}')
    if not out.is_dir():
        return
    for entry in out.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            entry.unlink()
        else:
            shutil.rmtree(entry)



def update_candidates(game):
    """Sibling update folders used by shadPS4/bbhost conventions, without changing the base game."""
    game = Path(game).resolve(strict=True)
    seen = set()
    out = []
    for suffix in ('-UPDATE', '-update', '-patch', '-PATCH'):
        candidate = game.parent / (game.name + suffix)
        key = str(candidate).casefold()
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def find_game_update(game):
    """A sibling Bloodborne 1.09 update folder for the selected game, or None."""
    game = Path(game).resolve(strict=True)
    try:
        base = parse_sfo((game / 'sce_sys' / 'param.sfo').read_bytes())
    except (OSError, ValueError):
        base = {}
    base_title = base.get('TITLE_ID')
    for candidate in update_candidates(game):
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        try:
            sfo = parse_sfo((candidate / 'sce_sys' / 'param.sfo').read_bytes())
        except (OSError, ValueError) as error:
            print(f'Game update overlay: ignoring {candidate.name}: unreadable param.sfo ({error})',
                  file=sys.stderr)
            continue
        title, version, category = sfo.get('TITLE_ID'), sfo.get('APP_VER'), sfo.get('CATEGORY')
        if base_title and title != base_title:
            print(f'Game update overlay: ignoring {candidate.name}: TITLE_ID {title or "?"} != {base_title}',
                  file=sys.stderr)
            continue
        if version != MOD_VERSION or category not in (None, 'gp'):
            print(f'Game update overlay: ignoring {candidate.name}: version {version or "?"}, '
                  f'category {category or "?"}', file=sys.stderr)
            continue
        if not (candidate / 'eboot.bin').is_file():
            print(f'Game update overlay: ignoring {candidate.name}: eboot.bin missing', file=sys.stderr)
            continue
        return candidate
    return None


def materialize_overlay_directory(directory):
    """Turn a directory symlink into a shallow directory of links, so children can be overridden."""
    directory = Path(directory)
    if directory.is_symlink():
        target = directory.resolve(strict=True)
        if not target.is_dir():
            raise ValueError(f'File/directory conflict at {directory}')
        directory.unlink()
        directory.mkdir()
        for entry in target.iterdir():
            (directory / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    elif directory.exists() and not directory.is_dir():
        raise ValueError(f'File/directory conflict at {directory}')
    else:
        directory.mkdir(exist_ok=True)


def build_update_overlay(game, update, out):
    """Symlink union of base + update, with every update file winning (PS4 update semantics)."""
    game = Path(game).resolve(strict=True)
    update = Path(update).resolve(strict=True)
    out = Path(out).resolve()
    clear(out, (game, update))
    out.mkdir(parents=True, exist_ok=True)
    for entry in game.iterdir():
        (out / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    replaced = added = 0
    for directory, dirs, files in os.walk(update, followlinks=False):
        source_dir = Path(directory)
        if any((source_dir / name).is_symlink() for name in [*dirs, *files]):
            raise ValueError(f'Update folder contains a symlink: {source_dir}')
        relative = source_dir.relative_to(update)
        parent = out
        for part in relative.parts:
            parent = mods.child(parent, part)
            materialize_overlay_directory(parent)
        for name in sorted(files):
            source = source_dir / name
            if not source.is_file():
                raise ValueError(f'Update entry is not a regular file: {source}')
            destination = mods.child(parent, name)
            existed = destination.exists() or destination.is_symlink()
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            elif existed:
                destination.unlink()
            destination.symlink_to(source)
            if existed:
                replaced += 1
            else:
                added += 1
    print(f'Game update overlay: {update.name}: {replaced} base files replaced, {added} added',
          file=sys.stderr)
    return out


def command_update_overlay(args):
    """Print the effective game folder: base, or a sibling 1.09 update layered over it."""
    game = Path(args.game).resolve(strict=True)
    try:
        update = find_game_update(game)
        if update is None:
            clear(args.out, (game,))
            print('Game update overlay: no valid sibling 1.09 update; using selected folder',
                  file=sys.stderr)
            print(game)
            return
        result = build_update_overlay(game, update, args.out)
        print(f'Game update overlay: using {update}', file=sys.stderr)
        print(result)
    except Exception as error:
        try:
            clear(args.out, (game,))
        except Exception:
            pass
        print(f'Game update overlay: failed, using selected folder: {type(error).__name__}: {error}',
              file=sys.stderr)
        print(game)


def command_apply(args):
    game = str(args.game)
    try:
        clear(args.out, (args.game, args.mods_dir))
        ordered, _, disabled = load_order(args.mods_dir, args.config)
        layers, names = [], []
        for name in ordered:
            if name in disabled:
                print(f'Mods: {name}: disabled, skipped', file=sys.stderr)
                continue
            problem = problem_of(Path(args.mods_dir) / name)
            if problem:
                print(f'Mods: {name}: invalid, skipped ({problem})', file=sys.stderr)
                continue
            layers.append((name, Path(args.mods_dir) / name))
            names.append(name)
        result = mods.build_overlay(args.game, args.out, layers) if layers else Path(args.game)
        print('Mods: ' + (f'{len(names)} applied: {", ".join(names)}' if names else 'none'), file=sys.stderr)
        game = str(result)
    except Exception as error:  # the game must start unmodded
        print(f'Mods: failed, starting without mods: {error}', file=sys.stderr)
    print(game)


# --- PS4 PKG installs ------------------------------------------------------------------------------

MIB, GIB = 1 << 20, 1 << 30
MOD_MARGIN, GAME_MARGIN = 256 * MIB, GIB  # free space wanted beyond the extracted size
MOD_VERSION = '01.09'
PKG_MAGIC = b'\x7fCNT'
ENCRYPTED = ("This PKG is encrypted for a retail PS4 and can't be read; "
             'only homebrew (fake) PKGs are supported')
LAST_NAMES = {'eboot.bin', 'sce_sys', 'param.sfo'}  # moved last by an update: an interrupted one still reads as the old version


class Failure(Exception):
    """A problem the app shows to the user (the message is the text)."""


def gigabytes(size):
    return f'{size / GIB:.1f} GB'


def progress(done, total, label):
    print(f'progress {done} {total} {label}', flush=True)


def parse_sfo(data):
    """param.sfo -> {key: str or int}; ValueError when it is not one."""
    try:
        if data[:4] != b'\0PSF':
            raise ValueError('not a param.sfo')
        _, key_start, data_start, count = struct.unpack_from('<4xIIII', data, 0)
        values = {}
        for index in range(count):
            key_offset, fmt, length, _max, data_offset = struct.unpack_from('<HHIII', data, 20 + 16 * index)
            end = data.index(b'\0', key_start + key_offset)
            key = data[key_start + key_offset:end].decode('ascii')
            raw = data[data_start + data_offset:data_start + data_offset + length]
            values[key] = (struct.unpack('<I', raw[:4])[0] if fmt == 0x0404
                           else raw.split(b'\0')[0].decode('utf-8', 'replace'))
        return values
    except (struct.error, ValueError, UnicodeDecodeError) as error:
        raise ValueError(f'param.sfo: {error}')


def read_pkg(path):
    """The PKG's header facts (big-endian): {'contentId', 'contentType', 'size', 'sfo'}; 'sfo' is the
    unencrypted param.sfo entry of the PKG parsed (None when it cannot be read). Failure for a file
    that is not a complete PS4 PKG."""
    try:
        file_size = os.path.getsize(path)
        with open(path, 'rb') as file:
            head = file.read(0x440)
            if head[:4] != PKG_MAGIC:
                raise Failure('This is not a PS4 PKG file')
            if len(head) < 0x440:
                raise Failure('This PKG file is incomplete')
            info = {'contentId': head[0x40:0x64].split(b'\0')[0].decode('ascii', 'replace'),
                    'contentType': struct.unpack_from('>I', head, 0x74)[0],
                    'size': struct.unpack_from('>Q', head, 0x430)[0], 'sfo': None}
            if info['size'] > file_size:
                raise Failure('This PKG file is incomplete (the download or copy was cut short)')
            count, table = struct.unpack_from('>I', head, 0x10)[0], struct.unpack_from('>I', head, 0x18)[0]
            try:
                file.seek(table)
                for _ in range(min(count, 4096)):
                    entry = file.read(32)
                    if len(entry) < 32:
                        break
                    identifier, offset, size = struct.unpack_from('>I', entry, 0)[0], *struct.unpack_from('>II', entry, 16)
                    if identifier == 0x1000 and 0 < size <= MIB:  # param.sfo
                        here = file.tell()
                        file.seek(offset)
                        info['sfo'] = parse_sfo(file.read(size))
                        file.seek(here)
                        break
            except (ValueError, OSError, struct.error):
                info['sfo'] = None
    except OSError as error:
        raise Failure(f"Can't read the PKG: {error.strerror or error}")
    return info


def free_space(path):
    return shutil.disk_usage(path).free


def remove_staging(parent, keep=None):
    """Delete the .staging-* folders (left by an install that was killed) of `parent`."""
    if not Path(parent).is_dir():
        return
    for entry in Path(parent).iterdir():
        if entry.name.startswith('.staging-') and entry != keep:
            if entry.is_symlink() or not entry.is_dir():
                entry.unlink()
            else:
                shutil.rmtree(entry, ignore_errors=True)


def new_staging(parent):
    while True:
        staging = Path(parent) / f'.staging-{secrets.token_hex(4)}'
        try:
            staging.mkdir()
            return staging
        except FileExistsError:
            continue


def extracted_root(staging):
    """The folder the tool made in `staging` (<TITLE_ID>), or None."""
    folders = [e for e in Path(staging).iterdir() if e.is_dir() and not e.is_symlink()]
    return folders[0] if len(folders) == 1 else None


def run_tool(tool, pkg, staging, margin, space_dir):
    """Run ps4-pkg-tool PKG STAGING, relaying its `Progress-bytes: done total` lines as `progress`.
    Failure when it fails or the disk is too small (judged when it reports the total)."""
    tool = Path(tool) if tool else None
    if tool is None or not tool.is_file() or not os.access(tool, os.X_OK):
        raise Failure('The PKG tool is missing from this installation (ps4-pkg-tool)')
    tail = collections.deque(maxlen=12)
    checked = False
    try:
        process = subprocess.Popen([str(tool), str(pkg), str(staging)], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
    except OSError as error:
        raise Failure(f"Can't run the PKG tool: {error.strerror or error}")
    try:
        for line in process.stdout:
            line = line.rstrip('\n')
            match = re.fullmatch(r'Progress-bytes: (\d+) (\d+)', line)
            if not match:
                if line.strip():
                    tail.append(line)
                    print(line, file=sys.stderr, flush=True)
                continue
            done, total = int(match[1]), int(match[2])
            if not checked and total:
                checked = True
                free = free_space(space_dir)
                if free < total + margin:
                    process.kill()
                    raise Failure(f'Not enough free space: {gigabytes(total + margin)} needed '
                                  f'(the files take {gigabytes(total)}), {gigabytes(free)} available')
            progress(done, total, 'Extracting')
        code = process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
    if code != 0:
        text = '\n'.join(tail)
        if free_space(space_dir) < 16 * MIB:
            raise Failure('Not enough free space to unpack the PKG')
        if 'file size is different' in text or 'bigger than pkg size' in text:
            raise Failure('This PKG file is damaged or incomplete')
        raise Failure(ENCRYPTED)
    return code


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as file:
        for block in iter(lambda: file.read(4 * MIB), b''):
            digest.update(block)
    return digest.hexdigest()


def same_as_game(extracted, game, relative):
    """True when the extracted file is byte-identical to the game's (case-insensitive path)."""
    target = Path(game)
    for part in Path(relative).parts:
        target = mods.child(target, part)
    if not target.is_file() or target.stat().st_size != extracted.stat().st_size:
        return False
    return sha256_of(extracted) == sha256_of(target)


def extracted_sfo(root):
    try:
        return parse_sfo((Path(root) / 'sce_sys' / 'param.sfo').read_bytes())
    except (OSError, ValueError):
        return None


def mod_identity_problem(sfo, content_id):
    """Why the PKG is not for Bloodborne 1.09 (None when it is). Without param.sfo only the content
    ID can tell the title."""
    title = (sfo or {}).get('TITLE_ID') or content_id[7:16]
    version = (sfo or {}).get('APP_VER')
    if title not in mods.SERIALS or (version is not None and version != MOD_VERSION):
        return f'This PKG is for {title} version {version or "?"}; only Bloodborne 1.09 mods are supported'
    return None


def valid_mod_name(name):
    name = name.strip()
    if (not name or name.startswith('.') or any(c in name for c in '/\\') or
            any(ord(c) < 32 for c in name) or len(name.encode('utf-8')) > 200):
        raise Failure('This name can\'t be used as a folder name')
    return name


def make_mod_folder(mods_dir, name):
    """Create <mods_dir>/<name>, or '<name> (2)', ... (case-insensitively unique) -> its path."""
    taken = {p.name.casefold() for p in Path(mods_dir).iterdir()}
    number = 1
    while True:
        candidate = name if number == 1 else f'{name} ({number})'
        number += 1
        if candidate.casefold() in taken:
            continue
        try:
            (Path(mods_dir) / candidate).mkdir()
            return Path(mods_dir) / candidate
        except FileExistsError:
            continue


def install_pkg(args, state):
    name = valid_mod_name(args.name)
    pkg = Path(args.pkg)
    info = read_pkg(pkg)
    early = mod_identity_problem(info['sfo'], info['contentId'])
    if early:
        raise Failure(early)
    mods_dir = Path(args.mods_dir)
    try:
        mods_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise Failure(f"Can't create the mods folder: {error.strerror or error}")
    remove_staging(mods_dir)
    need = info['size'] + MOD_MARGIN
    if free_space(mods_dir) < need:
        raise Failure(f'Not enough free space: {gigabytes(need)} needed, {gigabytes(free_space(mods_dir))} available')
    staging = state['staging'] = new_staging(mods_dir)
    run_tool(args.tool, pkg, staging, MOD_MARGIN, mods_dir)
    progress(0, 0, 'Checking')
    root = extracted_root(staging)
    sfo = extracted_sfo(root) if root else None
    if root is None or sfo is None:
        raise Failure(ENCRYPTED if info['sfo'] else 'This PKG has no param.sfo')
    early = mod_identity_problem(sfo, info['contentId'])
    if early:
        raise Failure(early)
    game = Path(args.game) if args.game else None
    changed = []
    for relative in ('eboot.bin', *(p.relative_to(root).as_posix()
                                    for p in sorted(mods.child(root, 'sce_module').rglob('*'))
                                    if p.is_file())):
        source = mods.child(root, 'eboot.bin') if relative == 'eboot.bin' else root / relative
        if not source.is_file():
            continue
        if game is None or not game.is_dir() or not (game / 'eboot.bin').is_file():
            raise Failure("This PKG contains the game's executable, so it has to be compared with your "
                          "game's: choose the game folder first")
        if not same_as_game(source, game, relative):
            changed.append(relative)
    if 'eboot.bin' in changed:
        raise Failure("This PKG changes the game's executable (eboot.bin), which isn't supported")
    if changed:
        raise Failure(f"This PKG changes the game's modules ({changed[0]}), which isn't supported")
    data = mods.child(root, 'dvdroot_ps4')
    if not data.is_dir():
        raise Failure('This PKG has no game data files (dvdroot_ps4)')
    target = make_mod_folder(mods_dir, name)
    try:
        os.rename(data, target / 'dvdroot_ps4')
        row, _ = inspect(mods_dir, target.name, True)
        if not row['valid'] or not row['files']:
            raise Failure(row['problem'] or 'This PKG has no game data files (dvdroot_ps4)')
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return {'ok': True, 'name': target.name, 'problem': None, 'mod': row}


def command_install_pkg(args):
    result = {'ok': False, 'name': None, 'problem': None, 'mod': None}
    state = {}
    try:
        result.update(install_pkg(args, state))
    except Failure as failure:
        result['problem'] = str(failure)
    except Exception as error:  # the app shows the message; never a traceback
        result['problem'] = f'Install failed: {type(error).__name__}: {error}'
    finally:
        if state.get('staging'):
            shutil.rmtree(state['staging'], ignore_errors=True)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def tree_bytes(folder):
    """The size of the files under `folder` (links not followed)."""
    total = 0
    for directory, _, files in os.walk(folder):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                pass
    return total


def merge_tree(source, target, moved=None):
    """Move the files of `source` over the folder `target` (rename each, replacing; same filesystem).
    Names match the existing ones case-insensitively; eboot.bin and sce_sys go last. `moved(size)`
    after each file."""
    target = Path(target)
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        target.unlink()
    target.mkdir(exist_ok=True)
    # One listing per folder (mods.child lists it for each name: an update's ~13,000 files took
    # many minutes over Android's shared storage).
    existing = {}
    for entry in target.iterdir():
        existing.setdefault(entry.name.casefold(), entry)
    for entry in sorted(Path(source).iterdir(), key=lambda e: (e.name.casefold() in LAST_NAMES, e.name)):
        destination = existing.get(entry.name.casefold(), target / entry.name)
        if entry.is_dir() and not entry.is_symlink():
            merge_tree(entry, destination, moved)
        else:
            size = entry.lstat().st_size
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            os.replace(entry, destination)
            if moved:
                moved(size)


class MoveProgress:
    """`progress` lines for merge_tree: bytes moved of `total`, every 32 MiB and at the end."""
    STEP = 32 * MIB

    def __init__(self, total, label):
        self.total, self.label, self.done, self.shown = total, label, 0, -1
        progress(0, total, label)

    def __call__(self, size):
        self.done += size
        if self.done - self.shown >= self.STEP or self.done >= self.total:
            self.shown = self.done
            progress(min(self.done, self.total), self.total, self.label)


def game_layout(dest, title, has_game):
    """(parent, game): where the staging goes and the folder of the game. `dest` is the game folder
    itself (it holds eboot.bin, or is named like a serial) or the folder that holds it."""
    dest = Path(dest)
    if (dest / 'eboot.bin').is_file() or dest.name in mods.SERIALS:
        if dest.name in mods.SERIALS and dest.name != title:
            raise Failure(f'This PKG is for {title}, but the folder {dest.name} is for another release')
        return dest.parent, dest
    return dest, dest / title


def install_game(args, state, result):
    pkg = Path(args.pkg)
    info = read_pkg(pkg)
    sfo = info['sfo']
    if sfo is None:
        raise Failure("This PKG is damaged: its param.sfo can't be read")

    def identify(sfo):
        title = sfo.get('TITLE_ID') or info['contentId'][7:16]
        category = sfo.get('CATEGORY')
        result.update(titleId=title, category=category if category in ('gd', 'gp') else None,
                      appVer=sfo.get('APP_VER'))
        if title not in mods.SERIALS:
            raise Failure(f"This PKG is for {title}, which isn't a Bloodborne release bbport supports")
        if category not in ('gd', 'gp'):
            raise Failure(f"This PKG is {category or 'unknown'} content; only the game (gd) and its "
                          'update (gp) can be installed')
        return title, category

    title, category = identify(sfo)
    parent, game = game_layout(args.dest, title, True)
    installed = (game / 'eboot.bin').is_file()
    if installed:
        have = None
        try:
            have = parse_sfo((game / 'sce_sys' / 'param.sfo').read_bytes()).get('TITLE_ID')
        except (OSError, ValueError):
            pass
        if have and have != title:
            raise Failure(f'The game in {game.name} is {have}, but this PKG is for {title}')
    if category == 'gd' and installed and not args.replace:
        raise Failure('A game is already installed there')
    if category == 'gp' and not installed:
        raise Failure(f'There is no game to update in {game}: install the base game PKG for {title} first')
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise Failure(f"Can't create the folder: {error.strerror or error}")
    remove_staging(parent)
    need = info['size'] + GAME_MARGIN
    if free_space(parent) < need:
        raise Failure(f'Not enough free space: {gigabytes(need)} needed, {gigabytes(free_space(parent))} available')
    staging = state['staging'] = new_staging(parent)
    run_tool(args.tool, pkg, staging, GAME_MARGIN, parent)
    progress(0, 0, 'Installing')
    root = extracted_root(staging)
    sfo = extracted_sfo(root) if root else None
    if root is None or sfo is None:
        raise Failure(ENCRYPTED if info['sfo'] else 'This PKG has no param.sfo')
    if (identify(sfo)) != (title, category):
        raise Failure('The PKG changed while it was unpacked')
    if category == 'gd':
        aside = None
        if game.exists() or game.is_symlink():
            aside = state['aside'] = parent / f'.staging-old-{secrets.token_hex(4)}'
            os.rename(game, aside)
        try:
            os.rename(root, game)
        except OSError:
            if aside is not None:
                os.rename(aside, game)
            raise
        result['gameDir'] = str(game)
    else:
        merge_tree(root, game, MoveProgress(tree_bytes(root), 'Installing'))
        result['gameDir'] = str(game)
    progress(0, 0, 'Checking')
    try:
        found = load_game_check().problem(game)
    except Exception as error:
        raise Failure(f"Couldn't check the game files: {type(error).__name__}: {error}")
    if found is None:
        result['ready'] = True
    elif found[0] == 'missing_update':
        result['next'] = f'Install the 1.09 update PKG for {title}'
    else:
        raise Failure(load_game_check().explain(*found))


def command_install_game(args):
    result = {'ok': False, 'gameDir': None, 'titleId': None, 'category': None, 'appVer': None,
              'ready': False, 'next': None, 'problem': None}
    state = {}
    try:
        install_game(args, state, result)
        result['ok'] = True
    except Failure as failure:
        result['problem'] = str(failure)
    except Exception as error:
        result['problem'] = f'Install failed: {type(error).__name__}: {error}'
    finally:
        for key in ('staging', 'aside'):
            if state.get(key):
                shutil.rmtree(state[key], ignore_errors=True)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def die_cleanly(signum, _frame):
    raise SystemExit(128 + signum)  # the installs' finally blocks remove their staging


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    for name, function in (('list', command_list), ('validate', command_validate), ('apply', command_apply)):
        sub = commands.add_parser(name)
        sub.set_defaults(function=function)
        sub.add_argument('--mods-dir', required=True, type=Path)
        if name != 'validate':
            sub.add_argument('--config', type=Path)
        if name == 'validate':
            sub.add_argument('--name', required=True)
        if name == 'apply':
            sub.add_argument('--game', required=True, type=Path)
            sub.add_argument('--out', required=True, type=Path)
    update = commands.add_parser('update-overlay')
    update.set_defaults(function=command_update_overlay)
    update.add_argument('--game', required=True, type=Path)
    update.add_argument('--out', required=True, type=Path)
    pkg = commands.add_parser('install-pkg')
    pkg.set_defaults(function=command_install_pkg)
    pkg.add_argument('--pkg', required=True, type=Path)
    pkg.add_argument('--name', required=True)
    pkg.add_argument('--mods-dir', required=True, type=Path)
    pkg.add_argument('--game', default='')
    pkg.add_argument('--tool', default='')
    game = commands.add_parser('install-game')
    game.set_defaults(function=command_install_game)
    game.add_argument('--pkg', required=True, type=Path)
    game.add_argument('--dest', required=True, type=Path)
    game.add_argument('--replace', action='store_true')
    game.add_argument('--tool', default='')
    args = parser.parse_args()
    if args.command in ('install-pkg', 'install-game'):
        signal.signal(signal.SIGTERM, die_cleanly)
        signal.signal(signal.SIGINT, die_cleanly)
    args.function(args)


if __name__ == '__main__':
    main()
