#!/usr/bin/env python3
"""Exercise native cache invalidation with tiny fake tools in temporary trees.

No real compiler, Cargo build, current build output or guest is invoked.
"""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.dont_write_bytecode = True
SOURCE = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("scarlet_build_inputs", SOURCE / "compat/scarlet/build_inputs.py")
inputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inputs)


class BuildFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="scarlet-build-input-test-")
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        for name in inputs.RECIPES:
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / name, destination)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.env = dict(os.environ, FAKE_LOG=str(self.root / "tool-calls.jsonl"))
        for name in inputs.ENVIRONMENT:
            path = self.tools / name
            path.mkdir()
            self.env[name] = str(path)
        compiler = self.tools / "cc"
        self.script(compiler, '''
import json, os, pathlib, sys
args = sys.argv[1:]
if args == ['-print-resource-dir']:
    print(pathlib.Path(__file__).parent)
else:
    with open(os.environ['FAKE_LOG'], 'a') as log:
        log.write(json.dumps({'tool':'cc', 'args':args})+'\\n')
    if os.environ.get('FAKE_FAIL'):
        sys.exit(42)
    pathlib.Path(args[args.index('-o')+1]).write_text('object '+repr(args))
''')
        archiver = self.tools / "ar"
        self.script(archiver, '''
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(json.dumps({'tool':'ar', 'args':args})+'\\n')
if args[0] == 't':
    print('native.o')
elif args[0] == 'p':
    sys.stdout.buffer.write(pathlib.Path(args[1]).read_bytes())
elif args[0] == 'rcs':
    pathlib.Path(args[1]).write_bytes(b''.join(pathlib.Path(p).read_bytes() for p in args[2:]))
else:
    sys.exit(2)
''')
        self.env.update(SCARLET_CC=str(compiler), SCARLET_AR=str(archiver))
        rustc = Path(self.env['SCARLET_RUST_TOOLCHAIN']) / "bin/rustc"
        self.script(rustc, "print('fake rustc for packaging test')\n")
        archive = self.root / "zlib-v1.tar"
        self.zlib_source(archive, "first")
        self.env['SCARLET_ZLIB_SOURCE'] = str(archive)

    def script(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/usr/bin/env python3\n' + body)
        path.chmod(0o755)

    def zlib_source(self, path, contents):
        names = ('adler32 compress crc32 deflate infback inffast inflate inftrees trees uncompr zutil').split()
        with tarfile.open(path, 'w') as archive:
            for name in [*(name + '.c' for name in names), 'zlib.h', 'zconf.h', 'LICENSE']:
                data = contents.encode()
                entry = tarfile.TarInfo('zlib/' + name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))

    def run_script(self, name, expected=0, environment=None):
        path = self.root / name
        command = ['/bin/sh', str(path)] if path.suffix == '.sh' else [sys.executable, '-B', str(path)]
        result = subprocess.run(command, cwd=self.root, env=environment or self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout.decode())
        return result.stdout.decode()

    def calls(self, tool):
        path = Path(self.env['FAKE_LOG'])
        return [] if not path.exists() else [entry for line in path.read_text().splitlines()
                                            if (entry := json.loads(line))['tool'] == tool]

    def snapshot(self, directory):
        return {str(path.relative_to(directory)): (inputs.sha256(path), path.stat().st_mtime_ns)
                for path in directory.rglob('*') if path.is_file()}

    def test_fingerprint_tracks_paths_and_contents_not_mtimes(self):
        first = inputs.fingerprint(self.root, self.env)
        recipe = self.root / 'compat/scarlet/cc.sh'
        os.utime(recipe, (1, 1))
        self.assertEqual(inputs.fingerprint(self.root, self.env), first)
        for variable in inputs.ENVIRONMENT:
            replacement = self.tools / ('new-' + variable)
            replacement.mkdir()
            self.assertNotEqual(inputs.fingerprint(self.root, {**self.env, variable:str(replacement)})['id'], first['id'])
        recipe.write_text(recipe.read_text() + '\n# changed recipe\n')
        self.assertNotEqual(inputs.fingerprint(self.root, self.env)['id'], first['id'])

    def test_zlib_cache_rebuild_and_failed_build_preserves_installation(self):
        script = 'compat/scarlet/build_zlib.py'
        self.run_script(script)
        prefix = self.root / '.scarlet-build/zlib'
        first = self.snapshot(prefix)
        self.assertEqual(len(self.calls('cc')), 11)
        self.assertIn('cached', self.run_script(script))
        self.assertEqual(self.snapshot(prefix), first)
        self.assertEqual(len(self.calls('cc')), 11)
        second_archive = self.root / 'zlib-v2.tar'
        self.zlib_source(second_archive, 'second')
        self.env['SCARLET_ZLIB_SOURCE'] = str(second_archive)
        self.run_script(script)
        self.assertEqual(len(self.calls('cc')), 22)
        self.assertEqual((prefix / 'include/zlib.h').read_text(), 'second')
        before_failure = self.snapshot(prefix)
        recipe = self.root / 'compat/scarlet/http/cc.sh'
        recipe.write_text(recipe.read_text() + '\n# invalidate\n')
        self.run_script(script, expected=1, environment={**self.env, 'FAKE_FAIL':'1'})
        self.assertEqual(self.snapshot(prefix), before_failure)
        self.assertEqual(list((self.root / '.scarlet-build').glob('.zlib-build-*')), [])
        self.run_script(script)
        self.assertEqual(json.loads((prefix / 'build-inputs.json').read_text()),
                         inputs.fingerprint(self.root, self.env))
        self.assertEqual(list((self.root / '.scarlet-build').glob('.zlib-build-*')), [])

    def test_http_flags_and_package_cache_follow_fingerprint(self):
        http = self.root / 'compat/scarlet/http'
        # Replace only the network/source preparation step in this disposable tree.
        (http / 'prepare.py').write_text('# fixture sources already prepared\n')
        archive = http / 'target/cargo/aarch64-unknown-scarlet/release/deps/native.rlib'
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b'native dependency objects')
        build_output = http / 'target/curl-build'
        headers = build_output / 'include/curl'
        headers.mkdir(parents=True)
        (headers / 'curl.h').write_text('fixture curl declarations\n')
        messages = [
            {'reason':'compiler-artifact', 'filenames':[str(archive)], 'manifest_path':str(http / 'Cargo.toml')},
            {'reason':'build-script-executed', 'package_id':'curl-sys', 'out_dir':str(build_output)},
        ]
        cargo = Path(self.env['SCARLET_RUST_TOOLCHAIN']) / 'bin/cargo'
        self.script(cargo, '''
import json, os
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(json.dumps({'tool':'cargo', 'id':os.environ['SCARLET_BUILD_ID'],
                         'cflags':os.environ['CFLAGS_aarch64_unknown_scarlet'],
                         'rustflags':os.environ['CARGO_ENCODED_RUSTFLAGS']})+'\\n')
for message in ''' + repr(messages) + ''':
    print(json.dumps(message))
''')
        zlib = self.root / '.scarlet-build/zlib/include'
        zlib.mkdir(parents=True)
        (zlib / 'zlib.h').write_text('fixture zlib declarations')
        script = 'compat/scarlet/http/build.sh'
        self.run_script(script)
        prefix = http / 'target/install'
        first = self.snapshot(prefix)
        archive_calls = len(self.calls('ar'))
        self.assertIn('cached', self.run_script(script))
        self.assertEqual(self.snapshot(prefix), first)
        self.assertEqual(len(self.calls('ar')), archive_calls)
        self.assertEqual(self.calls('cargo')[0], self.calls('cargo')[1])
        runtime = self.tools / 'runtime-v2'
        runtime.mkdir()
        self.env['SCARLET_RUNTIME'] = str(runtime)
        self.run_script(script)
        last = self.calls('cargo')[-1]
        self.assertNotEqual(last['id'], self.calls('cargo')[0]['id'])
        self.assertIn('-DSCARLET_BUILD_ID=scarlet_' + last['id'], last['cflags'])
        self.assertIn('-Cmetadata=scarlet_' + last['id'], last['rustflags'].split('\x1f'))
        self.assertGreater(len(self.calls('ar')), archive_calls)
        archive.write_bytes(b'changed dependency objects')
        self.run_script(script)
        self.assertEqual((prefix / 'lib/libcurl.a').read_bytes(), archive.read_bytes())


if __name__ == '__main__':
    unittest.main()
