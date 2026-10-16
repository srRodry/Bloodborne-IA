"""Tests for tools/android/mods_android.py (run: python3 -I tests/test_mods_android.py)."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'tools/android/mods_android.py'
spec = importlib.util.spec_from_file_location('mods_android', SCRIPT)
ma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ma)


class AndroidModTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = self.root / 'game'
        (self.game / 'dvdroot_ps4' / 'chr').mkdir(parents=True)
        (self.game / 'eboot.bin').write_bytes(b'eboot')
        (self.game / 'dvdroot_ps4' / 'chr' / 'a.dcx').write_bytes(b'orig')
        self.mods = self.root / 'mods'
        self.mods.mkdir()
        self.config = self.root / 'mods.json'
        self.out = self.root / 'mod-game'

    def put(self, mod, relative, data=b'x'):
        path = self.mods / mod / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def run_cli(self, *args):
        result = subprocess.run([sys.executable, '-I', str(SCRIPT), *map(str, args)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def listing(self):
        result = self.run_cli('list', '--mods-dir', self.mods, '--config', self.config)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        return json.loads(lines[0])

    def rows(self):
        return {m['name']: m for m in self.listing()['mods']}

    def apply(self):
        result = self.run_cli('apply', '--game', self.game, '--out', self.out,
                              '--mods-dir', self.mods, '--config', self.config)
        return Path(result.stdout.strip()), result.stderr

    def test_layouts_and_counts(self):
        self.put('plain', 'dvdroot_ps4/chr/a.dcx', b'12345')
        self.put('plain', 'dvdroot_ps4/chr/b.dcx', b'123')
        self.put('serial', 'CUSA03173/dvdroot_ps4/parts/p.dcx')
        self.put('bare', 'chr/c.dcx')
        self.put('wrapper', 'inner/dvdroot_ps4/msg/m.fmg')
        rows = self.rows()
        self.assertEqual(list(rows), ['bare', 'plain', 'serial', 'wrapper'])
        for row in rows.values():
            self.assertTrue(row['valid'], row)
            self.assertTrue(row['enabled'])
            self.assertIsNone(row['problem'])
        self.assertEqual((rows['plain']['files'], rows['plain']['bytes']), (2, 8))
        self.assertEqual(rows['bare']['files'], 1)
        data = self.listing()
        self.assertEqual(data['modsDir'], str(self.mods))
        self.assertTrue(data['exists'])
        self.assertIsNone(data['error'])
        self.assertEqual(set(rows['plain']), {'name', 'enabled', 'valid', 'problem', 'files', 'bytes',
                                              'overrides', 'overriddenBy'})

    def test_non_mod_folder_listed_invalid(self):
        self.put('docs', 'readme.txt')
        (self.mods / 'empty').mkdir()
        self.put('real', 'dvdroot_ps4/chr/a.dcx')
        (self.mods / 'loose.zip').write_bytes(b'file, not a folder')
        rows = self.rows()
        self.assertEqual(list(rows), ['real', 'docs', 'empty'])
        for name in ('docs', 'empty'):
            self.assertFalse(rows[name]['valid'])
            self.assertFalse(rows[name]['enabled'])
            self.assertEqual(rows[name]['problem'],
                             'Not a Bloodborne mod: no dvdroot_ps4 or game folders inside')

    def test_system_files_make_mod_invalid(self):
        self.put('exe', 'dvdroot_ps4/chr/a.dcx')
        self.put('exe', 'eboot.bin')
        self.put('sys', 'dvdroot_ps4/chr/a.dcx')
        self.put('sys', 'sce_sys/param.sfo')
        self.put('module', 'dvdroot_ps4/chr/a.dcx')
        self.put('module', 'sce_module/libc.prx')
        self.put('sfo', 'dvdroot_ps4/chr/a.dcx')
        self.put('sfo', 'param.sfo')
        self.put('wrapped', 'CUSA03173/dvdroot_ps4/chr/a.dcx')
        self.put('wrapped', 'CUSA03173/EBOOT.BIN')
        self.put('prx', 'dvdroot_ps4/chr/a.dcx')
        self.put('prx', 'patch.sprx')
        self.put('good', 'dvdroot_ps4/chr/b.dcx')
        self.put('good', 'readme.txt')
        rows = self.rows()
        for name in ('exe', 'sys', 'module', 'sfo', 'wrapped', 'prx'):
            self.assertFalse(rows[name]['valid'], name)
            self.assertIn('1.09', rows[name]['problem'])
            self.assertEqual(rows[name]['files'], 0)
        self.assertIn('eboot.bin', rows['exe']['problem'])
        self.assertIn('sce_sys', rows['sys']['problem'])
        self.assertTrue(rows['good']['valid'])
        # Enabled, but never applied.
        result, log = self.apply()
        self.assertIn('Mods: 1 applied: good', log)
        self.assertIn('exe: invalid, skipped', log)
        self.assertEqual({p.name for p in (result / 'dvdroot_ps4/chr').iterdir()}, {'a.dcx', 'b.dcx'})
        self.assertEqual((result / 'dvdroot_ps4/chr/a.dcx').read_bytes(), b'orig')

    def test_case_insensitive_conflicts_both_ways(self):
        self.put('A', 'dvdroot_ps4/chr/Texture.DCX', b'a')
        self.put('A', 'dvdroot_ps4/chr/only_a.dcx')
        self.put('B', 'dvdroot_ps4/CHR/texture.dcx', b'bb')
        self.put('B', 'dvdroot_ps4/chr/b.dcx')
        self.put('C', 'dvdroot_ps4/chr/texture.dcx', b'ccc')
        rows = self.rows()
        self.assertEqual(rows['A']['overriddenBy'], [{'mod': 'B', 'files': 1}, {'mod': 'C', 'files': 1}])
        self.assertEqual(rows['A']['overrides'], [])
        self.assertEqual(rows['B']['overrides'], [{'mod': 'A', 'files': 1}])
        self.assertEqual(rows['B']['overriddenBy'], [{'mod': 'C', 'files': 1}])
        self.assertEqual(rows['C']['overrides'], [{'mod': 'A', 'files': 1}, {'mod': 'B', 'files': 1}])
        # Reversing the order reverses the roles.
        self.config.write_text(json.dumps({'order': ['C', 'B', 'A']}))
        rows = self.rows()
        self.assertEqual(rows['A']['overrides'], [{'mod': 'C', 'files': 1}, {'mod': 'B', 'files': 1}])
        self.assertEqual(rows['C']['overriddenBy'], [{'mod': 'B', 'files': 1}, {'mod': 'A', 'files': 1}])

    def test_disabled_and_invalid_mods_do_not_conflict(self):
        self.put('A', 'dvdroot_ps4/chr/a.dcx')
        self.put('B', 'dvdroot_ps4/chr/a.dcx')
        self.put('Bad', 'dvdroot_ps4/chr/a.dcx')
        self.put('Bad', 'eboot.bin')
        self.config.write_text(json.dumps({'order': ['A', 'B', 'Bad'], 'disabled': ['B']}))
        rows = self.rows()
        self.assertFalse(rows['B']['enabled'])
        self.assertEqual(rows['B']['files'], 1)
        self.assertEqual(rows['A']['overriddenBy'], [])
        self.assertEqual(rows['B']['overrides'], [])
        self.assertEqual(rows['Bad']['overrides'], [])

    def test_config_order_and_new_mod(self):
        for name in ('A', 'B', 'C'):
            self.put(name, f'dvdroot_ps4/chr/{name}.dcx')
        self.config.write_text(json.dumps({'order': ['C', 'gone', 'A'], 'disabled': ['A']}))
        rows = self.listing()['mods']
        self.assertEqual([m['name'] for m in rows], ['C', 'A', 'B'])
        self.assertEqual([m['enabled'] for m in rows], [True, False, True])

    def test_bad_config_reports_error(self):
        self.put('A', 'dvdroot_ps4/chr/a.dcx')
        self.config.write_text('{not json')
        data = self.listing()
        self.assertIsNotNone(data['error'])
        self.assertEqual(data['mods'], [])
        self.assertEqual(self.apply()[0], self.game)  # unmodded start

    def test_missing_mods_dir(self):
        result = self.run_cli('list', '--mods-dir', self.root / 'nope', '--config', self.config)
        data = json.loads(result.stdout)
        self.assertEqual(data, {'modsDir': str(self.root / 'nope'), 'exists': False, 'mods': [], 'error': None})

    def test_validate_one(self):
        self.put('Bad', 'dvdroot_ps4/chr/a.dcx')
        self.put('Bad', 'sce_module/x.prx')
        self.put('Good', 'dvdroot_ps4/chr/a.dcx', b'1234')
        bad = json.loads(self.run_cli('validate', '--mods-dir', self.mods, '--name', 'Bad').stdout)
        good = json.loads(self.run_cli('validate', '--mods-dir', self.mods, '--name', 'Good').stdout)
        missing = json.loads(self.run_cli('validate', '--mods-dir', self.mods, '--name', 'Nope').stdout)
        self.assertFalse(bad['valid'])
        self.assertIn('sce_module', bad['problem'])
        self.assertEqual((good['valid'], good['files'], good['bytes']), (True, 1, 4))
        self.assertFalse(missing['valid'])

    def test_apply_overlay_order_and_skips(self):
        self.put('A', 'dvdroot_ps4/chr/a.dcx', b'A')
        self.put('B', 'dvdroot_ps4/chr/a.dcx', b'B')
        self.put('B', 'dvdroot_ps4/chr/new.dcx', b'new')
        self.put('Off', 'dvdroot_ps4/chr/a.dcx', b'Off')
        self.put('Bad', 'dvdroot_ps4/chr/a.dcx', b'Bad')
        self.put('Bad', 'eboot.bin')
        self.config.write_text(json.dumps({'order': ['A', 'B', 'Off', 'Bad'], 'disabled': ['Off']}))
        result, log = self.apply()
        self.assertEqual(result.parent, self.out.resolve())
        self.assertTrue(result.name.startswith('mod-game-'))
        self.assertEqual((result / 'dvdroot_ps4/chr/a.dcx').read_bytes(), b'B')
        self.assertEqual((result / 'dvdroot_ps4/chr/new.dcx').read_bytes(), b'new')
        self.assertEqual((result / 'eboot.bin').read_bytes(), b'eboot')
        self.assertEqual((self.game / 'dvdroot_ps4/chr/a.dcx').read_bytes(), b'orig')
        self.assertIn('Mods: 2 applied: A, B', log)
        self.assertIn('Off: disabled, skipped', log)
        self.assertIn('Bad: invalid, skipped', log)

    def test_apply_removes_stale_overlays(self):
        self.put('A', 'dvdroot_ps4/chr/a.dcx', b'A')
        stale = self.out / 'mod-game-stale' / 'dvdroot_ps4'
        stale.mkdir(parents=True)
        (stale / 'x').symlink_to(self.game / 'eboot.bin')
        (self.out / 'junk.txt').write_text('old')
        first, _ = self.apply()
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [first.name])
        second, _ = self.apply()
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [second.name])
        self.assertTrue((self.game / 'eboot.bin').exists())  # links were removed, not followed

    def test_apply_without_valid_mods_returns_game_and_clears(self):
        self.put('Bad', 'dvdroot_ps4/chr/a.dcx')
        self.put('Bad', 'eboot.bin')
        self.put('Off', 'dvdroot_ps4/chr/a.dcx')
        self.config.write_text(json.dumps({'disabled': ['Off']}))
        (self.out / 'mod-game-stale').mkdir(parents=True)
        result, log = self.apply()
        self.assertEqual(result, self.game)
        self.assertEqual(list(self.out.iterdir()), [])
        self.assertIn('Mods: none', log)

    def test_apply_missing_mods_dir_and_refuses_to_clear_game(self):
        result = self.run_cli('apply', '--game', self.game, '--out', self.out,
                              '--mods-dir', self.root / 'nope', '--config', self.config)
        self.assertEqual(Path(result.stdout.strip()), self.game)
        result = self.run_cli('apply', '--game', self.game, '--out', self.game,
                              '--mods-dir', self.mods, '--config', self.config)
        self.assertEqual(Path(result.stdout.strip()), self.game)
        self.assertIn('Refusing', result.stderr)
        self.assertTrue((self.game / 'eboot.bin').exists())

    def test_update_overlay_uses_sibling_109(self):
        (self.game / 'sce_sys').mkdir()
        (self.game / 'sce_sys/param.sfo').write_bytes(
            make_sfo({'TITLE_ID': 'CUSA03173', 'APP_VER': '01.00', 'CATEGORY': 'gd'}))
        (self.game / 'dvdroot_ps4/chr/keep.dcx').write_bytes(b'keep')
        update = self.root / 'game-UPDATE'
        (update / 'sce_sys').mkdir(parents=True)
        (update / 'dvdroot_ps4/chr').mkdir(parents=True)
        (update / 'dvdroot_ps4/msg').mkdir(parents=True)
        (update / 'sce_sys/param.sfo').write_bytes(
            make_sfo({'TITLE_ID': 'CUSA03173', 'APP_VER': '01.09', 'CATEGORY': 'gp'}))
        (update / 'eboot.bin').write_bytes(b'new eboot')
        (update / 'dvdroot_ps4/chr/a.dcx').write_bytes(b'updated')
        (update / 'dvdroot_ps4/msg/new.fmg').write_bytes(b'new')
        out = self.root / 'update-game'
        result = self.run_cli('update-overlay', '--game', self.game, '--out', out)
        effective = Path(result.stdout.strip())
        self.assertEqual(effective, out.resolve())
        self.assertEqual((effective / 'eboot.bin').read_bytes(), b'new eboot')
        self.assertEqual((effective / 'dvdroot_ps4/chr/a.dcx').read_bytes(), b'updated')
        self.assertEqual((effective / 'dvdroot_ps4/chr/keep.dcx').read_bytes(), b'keep')
        self.assertEqual((effective / 'dvdroot_ps4/msg/new.fmg').read_bytes(), b'new')
        self.assertEqual((self.game / 'eboot.bin').read_bytes(), b'eboot')
        self.assertIn('Game update overlay: using', result.stderr)

    def test_update_overlay_rejects_wrong_update(self):
        (self.game / 'sce_sys').mkdir()
        (self.game / 'sce_sys/param.sfo').write_bytes(
            make_sfo({'TITLE_ID': 'CUSA03173', 'APP_VER': '01.00', 'CATEGORY': 'gd'}))
        update = self.root / 'game-patch'
        (update / 'sce_sys').mkdir(parents=True)
        (update / 'sce_sys/param.sfo').write_bytes(
            make_sfo({'TITLE_ID': 'CUSA03173', 'APP_VER': '01.08', 'CATEGORY': 'gp'}))
        (update / 'eboot.bin').write_bytes(b'wrong')
        out = self.root / 'update-game'
        (out / 'stale').mkdir(parents=True)
        result = self.run_cli('update-overlay', '--game', self.game, '--out', out)
        self.assertEqual(Path(result.stdout.strip()), self.game.resolve())
        self.assertEqual(list(out.iterdir()), [])
        self.assertIn('version 01.08', result.stderr)

    def test_mods_py_still_agrees_on_selection(self):
        for name in ('A', 'B', 'C'):
            self.put(name, 'dvdroot_ps4/chr/a.dcx')
        self.config.write_text(json.dumps({'order': ['C'], 'disabled': ['B']}))
        enabled = [m['name'] for m in self.listing()['mods'] if m['enabled']]
        self.assertEqual(enabled, ma.mods.selected(self.mods, self.config))


# --- PS4 PKG installs -------------------------------------------------------------------------------

HOST_TOOL = ROOT / 'out/host/pkg-tool/ps4-pkg-tool'  # tools/android/build_pkg_tool.sh host
# A real 1.09 data-mod PKG (e.g. a translation) for the end-to-end tests: BB_TEST_MOD_PKG=<path>.
REAL_PKG = Path(os.environ.get('BB_TEST_MOD_PKG', '/nonexistent'))
REAL_EBOOT = ROOT / 'game/CUSA03173/eboot.bin'
REAL_GAME = ROOT / 'game/CUSA03173'


def make_sfo(values):
    """A param.sfo with string values."""
    keys, data, entries = b'', b'', b''
    for key, value in values.items():
        raw = value.encode() + b'\0'
        entries += struct.pack('<HHIII', len(keys), 0x0204, len(raw), len(raw), len(data))
        keys += key.encode() + b'\0'
        data += raw
    key_start = 20 + len(entries)
    data_start = (key_start + len(keys) + 3) & ~3
    return (b'\0PSF' + struct.pack('<IIII', 0x101, key_start, data_start, len(values)) + entries + keys +
            b'\0' * (data_start - key_start - len(keys)) + data)


def make_pkg(path, content_id='EP9000-CUSA03173_00-BLOODBORNE0000EU', sfo=None, padding=4096):
    """A PKG with a valid header and the param.sfo entry (what read_pkg looks at); no PFS image."""
    sfo = make_sfo(sfo) if sfo is not None else None
    head = bytearray(0x440)
    head[:4] = b'\x7fCNT'
    head[0x40:0x40 + len(content_id)] = content_id.encode()
    struct.pack_into('>I', head, 0x74, 0x1a)
    entries = b''
    if sfo is not None:
        struct.pack_into('>I', head, 0x10, 1)
        struct.pack_into('>I', head, 0x18, 0x440)
        entries = struct.pack('>IIIIII8x', 0x1000, 0, 0, 0, 0x440 + 32, len(sfo))
    body = bytes(head) + entries + (sfo or b'') + b'\0' * padding
    body = bytearray(body)
    struct.pack_into('>Q', body, 0x430, len(body))
    Path(path).write_bytes(bytes(body))
    return Path(path)


FAKE_TOOL = """#!%s
import json, os, signal, sys
plan = json.loads(os.environ['FAKE_PLAN'])
pkg, out = sys.argv[1:3]
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(out + '\\n')
if plan.get('print_first'):
    print(plan['print_first'], flush=True)
print('Progress-bytes: 0 %%d' %% plan.get('total', 100), flush=True)
if plan.get('signal'):
    os.kill(os.getpid(), plan['signal'])
for relative, content in plan.get('files', {}).items():
    path = os.path.join(out, plan.get('dir', 'CUSA03173'), relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as file:
        file.write(content.encode('latin-1'))
print('Progress-bytes: %%d %%d' %% (plan.get('total', 100), plan.get('total', 100)), flush=True)
sys.exit(plan.get('exit', 0))
""" % sys.executable


def sfo_file(title='CUSA03173', version='01.09', category='gp'):
    return make_sfo({'TITLE_ID': title, 'APP_VER': version, 'CATEGORY': category}).decode('latin-1')


class PkgTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mods = self.root / 'mods'
        self.tool = self.root / 'fake-tool'
        self.tool.write_text(FAKE_TOOL)
        self.tool.chmod(0o755)
        self.log = self.root / 'tool.log'
        patcher = mock.patch.dict(os.environ, {'FAKE_LOG': str(self.log), 'BB_SKIP_GAME_CHECK': ''})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pkg = self.root / 'a.pkg'

    def plan(self, **plan):
        os.environ['FAKE_PLAN'] = json.dumps(plan)

    def tool_runs(self):
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0

    def run_cli(self, *args):
        result = subprocess.run([sys.executable, '-I', str(SCRIPT), *map(str, args)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        progress = [line for line in lines[:-1]]
        return json.loads(lines[-1]), progress, result.stderr


class InstallModPkgTests(PkgTestCase):
    def setUp(self):
        super().setUp()
        self.game = self.root / 'game'
        (self.game / 'sce_module').mkdir(parents=True)
        (self.game / 'eboot.bin').write_bytes(b'eboot')
        (self.game / 'sce_module' / 'libc.prx').write_bytes(b'libc')
        make_pkg(self.pkg, sfo={'TITLE_ID': 'CUSA03173', 'APP_VER': '01.09', 'CATEGORY': 'gp'})

    def install(self, name='Mod', game=True, pkg=None):
        args = ['install-pkg', '--pkg', pkg or self.pkg, '--name', name, '--mods-dir', self.mods,
                '--tool', self.tool]
        if game:
            args += ['--game', self.game]
        return self.run_cli(*args)

    def good_files(self, **extra):
        return {'sce_sys/param.sfo': sfo_file(), 'dvdroot_ps4/msg/menu.fmg': 'text',
                'dvdroot_ps4/chr/a.dcx': 'dcx', **extra}

    def test_installs_mod_and_cleans_up(self):
        self.plan(files=self.good_files(**{'eboot.bin': 'eboot', 'sce_module/libc.prx': 'libc',
                                           'sce_sys/icon0.png': 'x'}), total=5000)
        result, progress, _ = self.install()
        self.assertEqual(result['ok'], True, result)
        self.assertEqual((result['name'], result['problem']), ('Mod', None))
        self.assertTrue(result['mod']['valid'])
        self.assertEqual((result['mod']['files'], result['mod']['name']), (2, 'Mod'))
        self.assertEqual(sorted(p.name for p in self.mods.iterdir()), ['Mod'])
        self.assertEqual([p.name for p in (self.mods / 'Mod').iterdir()], ['dvdroot_ps4'])
        self.assertEqual((self.mods / 'Mod/dvdroot_ps4/msg/menu.fmg').read_bytes(), b'text')
        self.assertIn('progress 5000 5000 Extracting', progress)
        self.assertTrue(all(line.startswith('progress ') for line in progress), progress)
        listed = self.run_cli('list', '--mods-dir', self.mods)[0]
        self.assertEqual([m['name'] for m in listed['mods']], ['Mod'])

    def test_name_made_unique_and_bad_names_refused(self):
        self.plan(files=self.good_files())
        names = [self.install('Mod')[0]['name'] for _ in range(3)]
        self.assertEqual(names, ['Mod', 'Mod (2)', 'Mod (3)'])
        self.assertEqual(self.install('mod')[0]['name'], 'mod (4)')  # case-insensitive filesystems
        runs = self.tool_runs()
        for bad in ('a/b', '.hidden', '', '..', 'x\\y'):
            result = self.install(bad)[0]
            self.assertFalse(result['ok'], bad)
            self.assertIsNone(result['name'])
            self.assertIn('name', result['problem'])
        self.assertEqual(self.tool_runs(), runs)
        self.assertEqual(self.install('Tiếng Việt')[0]['name'], 'Tiếng Việt')

    def test_wrong_version_and_title_are_refused_before_unpacking(self):
        self.plan(files=self.good_files())
        make_pkg(self.pkg, sfo={'TITLE_ID': 'CUSA03173', 'APP_VER': '01.00', 'CATEGORY': 'gd'})
        result = self.install()[0]
        self.assertEqual(result, {'ok': False, 'name': None, 'mod': None, 'problem':
                                  'This PKG is for CUSA03173 version 01.00; only Bloodborne 1.09 mods are supported'})
        make_pkg(self.pkg, sfo={'TITLE_ID': 'CUSA12345', 'APP_VER': '01.09'}, content_id='EP0000-CUSA12345_00-X')
        self.assertIn('CUSA12345 version 01.09', self.install()[0]['problem'])
        self.assertEqual(self.tool_runs(), 0)
        self.assertFalse(self.mods.exists())

    def test_identity_checked_again_on_the_extracted_param_sfo(self):
        make_pkg(self.pkg)  # no param.sfo entry in the PKG: only the content ID is known
        self.plan(files=self.good_files(**{'sce_sys/param.sfo': sfo_file(version='01.06')}))
        result = self.install()[0]
        self.assertIn('version 01.06', result['problem'])
        self.plan(files=self.good_files(**{'sce_sys/param.sfo': sfo_file(title='CUSA99999')}))
        self.assertIn('This PKG is for CUSA99999', self.install()[0]['problem'])
        self.assertEqual(sorted(p.name for p in self.mods.iterdir()), [])

    def test_executable_must_be_the_games(self):
        self.plan(files=self.good_files(**{'eboot.bin': 'other eboot'}))
        result = self.install()[0]
        self.assertFalse(result['ok'])
        self.assertEqual(result['problem'], "This PKG changes the game's executable (eboot.bin), which isn't supported")
        self.assertEqual(list(self.mods.iterdir()), [])
        self.plan(files=self.good_files(**{'eboot.bin': 'eboot'}))  # same
        self.assertTrue(self.install()[0]['ok'])
        self.plan(files=self.good_files(**{'eboot.bin': 'eboot', 'sce_module/libc.prx': 'patched'}))
        result = self.install('M2')[0]
        self.assertIn("modules (sce_module/libc.prx)", result['problem'])
        self.plan(files=self.good_files(**{'sce_module/new.prx': 'new'}))
        self.assertIn('sce_module/new.prx', self.install('M3')[0]['problem'])
        self.assertEqual([p.name for p in self.mods.iterdir()], ['Mod'])

    def test_eboot_needs_the_game_folder(self):
        self.plan(files=self.good_files(**{'eboot.bin': 'eboot'}))
        for game in (False,):
            result = self.install(game=game)[0]
            self.assertFalse(result['ok'])
            self.assertIn('choose the game folder first', result['problem'])
        (self.game / 'eboot.bin').unlink()
        self.assertIn('choose the game folder first', self.install()[0]['problem'])
        # Without an eboot.bin in the PKG the game is not needed.
        self.plan(files=self.good_files())
        self.assertTrue(self.install(game=False)[0]['ok'])

    def test_no_data_files(self):
        self.plan(files={'sce_sys/param.sfo': sfo_file(), 'eboot.bin': 'eboot'})
        result = self.install()[0]
        self.assertEqual(result['problem'], 'This PKG has no game data files (dvdroot_ps4)')
        self.assertEqual(list(self.mods.iterdir()), [])

    def test_tool_failures(self):
        retail = ("This PKG is encrypted for a retail PS4 and can't be read; "
                  'only homebrew (fake) PKGs are supported')
        for plan in ({'exit': 1}, {'signal': 11}, {'exit': 1, 'print_first': 'Failed extracting file index 3: x'}):
            self.plan(files=self.good_files(), **plan)
            result = self.install()[0]
            self.assertEqual((result['ok'], result['name'], result['problem']), (False, None, retail), plan)
            self.assertEqual(list(self.mods.iterdir()), [])  # staging removed
        self.plan(exit=1, print_first='Failed to open PKG: PKG file size is different')
        self.assertIn('damaged or incomplete', self.install()[0]['problem'])
        missing = self.run_cli('install-pkg', '--pkg', self.pkg, '--name', 'M', '--mods-dir', self.mods,
                               '--tool', self.root / 'nope')[0]
        self.assertIn('PKG tool is missing', missing['problem'])

    def test_not_enough_space(self):
        self.plan(files=self.good_files(), total=1 << 60)
        result = self.install()[0]
        self.assertFalse(result['ok'])
        self.assertTrue(result['problem'].startswith('Not enough free space'), result)
        self.assertEqual(list(self.mods.iterdir()), [])
        with mock.patch.object(ma, 'free_space', return_value=1 << 20):
            args = mock.Mock(pkg=self.pkg, name='x', mods_dir=self.mods, tool=self.tool, game='')
            args.name = 'x'
            with self.assertRaises(ma.Failure) as raised:
                ma.install_pkg(args, {})
            self.assertIn('Not enough free space', str(raised.exception))

    def test_bad_pkg_files(self):
        (self.root / 'x.pkg').write_bytes(b'PK\3\4 not a pkg' + b'\0' * 2000)
        self.assertEqual(self.install(pkg=self.root / 'x.pkg')[0]['problem'], 'This is not a PS4 PKG file')
        data = self.pkg.read_bytes()
        (self.root / 'cut.pkg').write_bytes(data[:len(data) - 100])
        self.assertIn('incomplete', self.install(pkg=self.root / 'cut.pkg')[0]['problem'])
        (self.root / 'tiny.pkg').write_bytes(b'\x7fCNT')
        self.assertFalse(self.install(pkg=self.root / 'tiny.pkg')[0]['ok'])
        self.assertFalse(self.install(pkg=self.root / 'missing.pkg')[0]['ok'])
        self.assertEqual(self.tool_runs(), 0)

    def test_stale_staging_removed_and_hidden_from_list(self):
        stale = self.mods / '.staging-dead' / 'CUSA03173' / 'dvdroot_ps4' / 'msg'
        stale.mkdir(parents=True)
        (stale / 'm.fmg').write_bytes(b'x')
        # A leftover staging folder has the layout of a mod, but is neither listed nor applied.
        self.assertEqual(self.run_cli('list', '--mods-dir', self.mods)[0]['mods'], [])
        game = self.root / 'g2'
        (game / 'dvdroot_ps4').mkdir(parents=True)
        applied = subprocess.run([sys.executable, '-I', str(SCRIPT), 'apply', '--game', game, '--out',
                                  self.root / 'o', '--mods-dir', self.mods], capture_output=True, text=True)
        self.assertEqual(Path(applied.stdout.strip()), game)
        self.plan(files=self.good_files())
        self.assertTrue(self.install()[0]['ok'])
        self.assertEqual(sorted(p.name for p in self.mods.iterdir()), ['Mod'])


class InstallGameTests(PkgTestCase):
    def setUp(self):
        super().setUp()
        self.dest = self.root / 'games'
        os.environ['BB_SKIP_GAME_CHECK'] = '1'
        self.base = make_pkg(self.root / 'base.pkg', sfo={'TITLE_ID': 'CUSA03173', 'APP_VER': '01.00', 'CATEGORY': 'gd'})
        self.update = make_pkg(self.root / 'update.pkg', sfo={'TITLE_ID': 'CUSA03173', 'APP_VER': '01.09', 'CATEGORY': 'gp'})

    def install(self, pkg, *extra, dest=None):
        return self.run_cli('install-game', '--pkg', pkg, '--dest', dest or self.dest, '--tool', self.tool, *extra)

    def base_files(self, **extra):
        return {'sce_sys/param.sfo': sfo_file(version='01.00', category='gd'), 'eboot.bin': 'old eboot',
                'dvdroot_ps4/chr/a.dcx': 'old a', 'dvdroot_ps4/chr/keep.dcx': 'keep', **extra}

    def update_files(self, **extra):
        return {'sce_sys/param.sfo': sfo_file(), 'eboot.bin': 'new eboot', 'dvdroot_ps4/chr/a.dcx': 'new a',
                'dvdroot_ps4/msg/new.fmg': 'new', **extra}

    def test_base_then_update_merge(self):
        self.plan(files=self.base_files(), total=2000)
        result, progress, _ = self.install(self.base)
        game = self.dest / 'CUSA03173'
        self.assertEqual(result, {'ok': True, 'gameDir': str(game), 'titleId': 'CUSA03173', 'category': 'gd',
                                  'appVer': '01.00', 'ready': True, 'next': None, 'problem': None})
        self.assertIn('progress 2000 2000 Extracting', progress)
        self.assertEqual((game / 'eboot.bin').read_bytes(), b'old eboot')
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ['CUSA03173'])  # no staging left
        self.plan(files=self.update_files())
        result = self.install(self.update)[0]
        self.assertEqual((result['ok'], result['category'], result['appVer'], result['problem']), (True, 'gp', '01.09', None))
        self.assertEqual(result['gameDir'], str(game))
        self.assertEqual((game / 'eboot.bin').read_bytes(), b'new eboot')
        self.assertEqual((game / 'dvdroot_ps4/chr/a.dcx').read_bytes(), b'new a')
        self.assertEqual((game / 'dvdroot_ps4/chr/keep.dcx').read_bytes(), b'keep')
        self.assertEqual((game / 'dvdroot_ps4/msg/new.fmg').read_bytes(), b'new')
        self.assertIn(b'APP_VER', (game / 'sce_sys/param.sfo').read_bytes())
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ['CUSA03173'])
        # Giving the game folder itself as the destination works too.
        self.plan(files=self.update_files(**{'dvdroot_ps4/msg/second.fmg': 'two'}))
        self.assertTrue(self.install(self.update, dest=game)[0]['ok'])
        self.assertTrue((game / 'dvdroot_ps4/msg/second.fmg').is_file())
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ['CUSA03173'])

    def test_update_matches_existing_names_case_insensitively(self):
        game = self.dest / 'CUSA03173'
        (game / 'DVDROOT_PS4' / 'Chr').mkdir(parents=True)
        (game / 'eboot.bin').write_bytes(b'old')
        (game / 'DVDROOT_PS4' / 'Chr' / 'a.dcx').write_bytes(b'old a')
        self.plan(files=self.update_files())
        self.assertTrue(self.install(self.update)[0]['ok'])
        self.assertEqual([p.name for p in game.iterdir() if p.name.lower() == 'dvdroot_ps4'], ['DVDROOT_PS4'])
        self.assertEqual((game / 'DVDROOT_PS4/Chr/a.dcx').read_bytes(), b'new a')

    def test_update_reports_moving_progress(self):
        self.plan(files=self.base_files())
        self.assertTrue(self.install(self.base)[0]['ok'])
        files = self.update_files()
        self.plan(files=files)
        result, progress, _ = self.install(self.update)
        self.assertTrue(result['ok'])
        total = sum(len(v.encode() if isinstance(v, str) else v) for v in files.values())
        self.assertIn(f'progress 0 {total} Installing', progress)
        self.assertIn(f'progress {total} {total} Installing', progress)

    def test_update_without_game(self):
        self.plan(files=self.update_files())
        result = self.install(self.update)[0]
        self.assertFalse(result['ok'])
        self.assertIsNone(result['gameDir'])
        self.assertIn('install the base game PKG for CUSA03173 first', result['problem'])
        self.assertEqual((result['titleId'], result['category']), ('CUSA03173', 'gp'))
        self.assertEqual(self.tool_runs(), 0)

    def test_wrong_title_and_category(self):
        make_pkg(self.pkg, content_id='EP0000-CUSA12345_00-X', sfo={'TITLE_ID': 'CUSA12345', 'APP_VER': '01.00', 'CATEGORY': 'gd'})
        result = self.install(self.pkg)[0]
        self.assertFalse(result['ok'])
        self.assertIn('CUSA12345', result['problem'])
        make_pkg(self.pkg, sfo={'TITLE_ID': 'CUSA03173', 'APP_VER': '01.00', 'CATEGORY': 'ac'})
        self.assertIn('ac content', self.install(self.pkg)[0]['problem'])
        make_pkg(self.pkg)  # no param.sfo readable
        self.assertFalse(self.install(self.pkg)[0]['ok'])
        # The folder of another release.
        other = self.root / 'CUSA00900'
        self.assertIn('another release', self.install(self.base, dest=other)[0]['problem'])
        self.assertEqual(self.tool_runs(), 0)
        self.assertFalse(other.exists())

    def test_param_sfo_after_unpacking_must_agree(self):
        self.plan(files=self.base_files(**{'sce_sys/param.sfo': sfo_file(title='CUSA00900', category='gd')}))
        result = self.install(self.base)[0]
        self.assertFalse(result['ok'])
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_replace_refusal_and_replace(self):
        self.plan(files=self.base_files())
        self.assertTrue(self.install(self.base)[0]['ok'])
        game = self.dest / 'CUSA03173'
        (game / 'dvdroot_ps4/chr/stale.dcx').write_bytes(b'stale')
        runs = self.tool_runs()
        result = self.install(self.base)[0]
        self.assertEqual((result['ok'], result['problem']), (False, 'A game is already installed there'))
        self.assertEqual(self.tool_runs(), runs)
        self.assertTrue((game / 'dvdroot_ps4/chr/stale.dcx').exists())
        self.plan(files=self.base_files(**{'dvdroot_ps4/chr/b.dcx': 'b'}))
        result = self.install(self.base, '--replace')[0]
        self.assertTrue(result['ok'], result)
        self.assertFalse((game / 'dvdroot_ps4/chr/stale.dcx').exists())
        self.assertTrue((game / 'dvdroot_ps4/chr/b.dcx').exists())
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ['CUSA03173'])

    def test_failed_replace_keeps_the_old_game(self):
        self.plan(files=self.base_files())
        self.install(self.base)
        self.plan(files=self.base_files(), exit=1)
        result = self.install(self.base, '--replace')[0]
        self.assertFalse(result['ok'])
        self.assertEqual((self.dest / 'CUSA03173/eboot.bin').read_bytes(), b'old eboot')
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ['CUSA03173'])

    def test_not_enough_space(self):
        self.plan(files=self.base_files(), total=1 << 60)
        result = self.install(self.base)[0]
        self.assertFalse(result['ok'])
        self.assertRegex(result['problem'], r'Not enough free space: .* GB needed .*GB available')
        self.assertEqual(list(self.dest.iterdir()), [])
        with mock.patch.object(ma, 'free_space', return_value=1 << 20):  # before the tool runs
            runs = self.tool_runs()
            args = mock.Mock(pkg=self.base, dest=self.dest, tool=self.tool, replace=False)
            with self.assertRaises(ma.Failure) as raised:
                ma.install_game(args, {}, {})
            self.assertIn('Not enough free space', str(raised.exception))
            self.assertEqual(self.tool_runs(), runs)

    def test_tool_failure(self):
        self.plan(files=self.base_files(), signal=11)
        result = self.install(self.base)[0]
        self.assertFalse(result['ok'])
        self.assertIn('encrypted for a retail PS4', result['problem'])
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_stale_staging_removed(self):
        (self.dest / '.staging-old').mkdir(parents=True)
        (self.dest / '.staging-old' / 'f').write_bytes(b'x')
        self.plan(files=self.base_files())
        self.assertTrue(self.install(self.base)[0]['ok'])
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ['CUSA03173'])

    def run_in_process(self, pkg, **options):
        out = io.StringIO()
        args = mock.Mock(pkg=pkg, dest=self.dest, tool=self.tool, replace=False, **options)
        with contextlib.redirect_stdout(out):
            ma.command_install_game(args)
        return json.loads(out.getvalue().splitlines()[-1])

    def test_ready_and_next_follow_game_check(self):
        class Checker:
            result = ('missing_update', 'CUSA03173', '01.00')

            def problem(self, game):
                return self.result

            def explain(self, kind, title, version):
                return f'{kind}: {title} {version}'

        checker = Checker()
        with mock.patch.object(ma, 'load_game_check', return_value=checker):
            self.plan(files=self.base_files())
            result = self.run_in_process(self.base)
            self.assertEqual((result['ok'], result['ready'], result['next'], result['problem']),
                             (True, False, 'Install the 1.09 update PKG for CUSA03173', None))
            self.assertEqual(result['gameDir'], str(self.dest / 'CUSA03173'))
            self.plan(files=self.update_files())
            checker.result = None
            result = self.run_in_process(self.update)
            self.assertEqual((result['ok'], result['ready'], result['next']), (True, True, None))
            checker.result = ('damaged_files', 'CUSA03173', '01.09')
            result = self.run_in_process(self.update)
            self.assertEqual((result['ok'], result['ready'], result['problem']), (False, False, 'damaged_files: CUSA03173 01.09'))
            self.assertEqual(result['gameDir'], str(self.dest / 'CUSA03173'))

    def test_game_check_recognises_the_real_executable(self):
        if not REAL_EBOOT.is_file():
            self.skipTest('no game eboot.bin')
        game = self.dest / 'CUSA03173'
        (game / 'sce_sys').mkdir(parents=True)
        shutil.copy(REAL_EBOOT, game / 'eboot.bin')
        (game / 'sce_sys/param.sfo').write_bytes(make_sfo({'TITLE_ID': 'CUSA03173', 'APP_VER': '01.00'}))
        os.environ['BB_SKIP_GAME_CHECK'] = ''
        self.plan(files={'sce_sys/param.sfo': sfo_file(), 'dvdroot_ps4/msg/new.fmg': 'new'})
        result = self.install(self.update)[0]
        self.assertEqual((result['ok'], result['ready'], result['problem']), (True, True, None), result)
        # A different executable is reported by game_check, not hidden.
        self.plan(files={'sce_sys/param.sfo': sfo_file(), 'eboot.bin': 'not a SELF'})
        result = self.install(self.update)[0]
        self.assertEqual((result['ok'], result['ready']), (False, False))
        self.assertIn('eboot.bin could not be read', result['problem'])


@unittest.skipUnless(HOST_TOOL.is_file() and REAL_PKG.is_file(), 'needs the host ps4-pkg-tool and BB_TEST_MOD_PKG')
class RealPkgTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_cli(self, *args):
        result = subprocess.run([sys.executable, '-I', str(SCRIPT), *map(str, args)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        return json.loads(lines[-1]), lines[:-1]

    @unittest.skipUnless(REAL_GAME.is_dir(), 'needs the game folder')
    def test_mod_install_and_apply(self):
        mods = self.root / 'mods'
        result, progress = self.run_cli('install-pkg', '--pkg', REAL_PKG, '--name', 'Vietnamese', '--mods-dir', mods,
                                        '--game', REAL_GAME, '--tool', HOST_TOOL)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['name'], 'Vietnamese')
        self.assertGreater(result['mod']['files'], 300)
        self.assertEqual([p.name for p in mods.iterdir()], ['Vietnamese'])
        self.assertTrue(progress and progress[-1].startswith('progress 0 0 Checking'), progress)
        self.assertTrue(any(line.endswith(' Extracting') for line in progress))
        out = self.root / 'mod-game'
        applied = subprocess.run([sys.executable, '-I', str(SCRIPT), 'apply', '--game', REAL_GAME, '--out', out,
                                  '--mods-dir', mods], capture_output=True, text=True)
        overlay = Path(applied.stdout.strip())
        self.assertEqual(overlay.parent, out.resolve(), applied.stderr)
        msg = overlay / 'dvdroot_ps4/msg'
        mod_msgs = [p for p in (mods / 'Vietnamese/dvdroot_ps4/msg').rglob('*') if p.is_file()]
        self.assertTrue(mod_msgs)
        for source in mod_msgs:
            link = msg / source.relative_to(mods / 'Vietnamese/dvdroot_ps4/msg')
            self.assertEqual(link.resolve(), source.resolve())
        game_file = next(p for p in (REAL_GAME / 'dvdroot_ps4/param').rglob('*') if p.is_file())
        mod_files = {p.relative_to(mods / 'Vietnamese/dvdroot_ps4') for p in (mods / 'Vietnamese/dvdroot_ps4').rglob('*') if p.is_file()}
        untouched = next(p for p in (REAL_GAME / 'dvdroot_ps4').rglob('*')
                         if p.is_file() and p.relative_to(REAL_GAME / 'dvdroot_ps4') not in mod_files)
        self.assertEqual((overlay / 'dvdroot_ps4' / untouched.relative_to(REAL_GAME / 'dvdroot_ps4')).resolve(), untouched.resolve())
        self.assertTrue(game_file.exists())
        self.assertEqual((overlay / 'eboot.bin').resolve(), (REAL_GAME / 'eboot.bin').resolve())

    @unittest.skipUnless(REAL_EBOOT.is_file(), 'needs the game eboot.bin')
    def test_update_pkg_merged_into_a_small_fake_base(self):
        dest = self.root / 'games'
        game = dest / 'CUSA03173'
        (game / 'sce_sys').mkdir(parents=True)
        (game / 'dvdroot_ps4/msg').mkdir(parents=True)
        shutil.copy(REAL_EBOOT, game / 'eboot.bin')
        (game / 'sce_sys/param.sfo').write_bytes(make_sfo({'TITLE_ID': 'CUSA03173', 'APP_VER': '01.00'}))
        (game / 'dvdroot_ps4/msg/old.fmg').write_bytes(b'old')
        result, progress = self.run_cli('install-game', '--pkg', REAL_PKG, '--dest', dest, '--tool', HOST_TOOL)
        self.assertEqual(result, {'ok': True, 'gameDir': str(game), 'titleId': 'CUSA03173', 'category': 'gp',
                                  'appVer': '01.09', 'ready': True, 'next': None, 'problem': None})
        self.assertTrue((game / 'dvdroot_ps4/msg/old.fmg').exists())
        self.assertGreater(len(list((game / 'dvdroot_ps4').rglob('*.*'))), 300)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ['CUSA03173'])
        self.assertTrue(any(line.endswith(' Extracting') for line in progress))
        self.assertEqual(ma.parse_sfo((game / 'sce_sys/param.sfo').read_bytes())['APP_VER'], '01.09')


class SfoTests(unittest.TestCase):
    def test_parse(self):
        values = ma.parse_sfo(make_sfo({'TITLE_ID': 'CUSA03173', 'APP_VER': '01.09', 'CATEGORY': 'gp'}))
        self.assertEqual(values, {'TITLE_ID': 'CUSA03173', 'APP_VER': '01.09', 'CATEGORY': 'gp'})
        for bad in (b'', b'\0PSF', b'nope' * 10):
            with self.assertRaises(ValueError):
                ma.parse_sfo(bad)


if __name__ == '__main__':
    unittest.main()
