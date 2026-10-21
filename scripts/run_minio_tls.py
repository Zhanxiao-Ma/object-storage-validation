#!/usr/bin/env python3
"""Run real Java/C++ S3 workers against local MinIO with verified private-CA TLS."""
import argparse
from collections import Counter
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from osv import cli
from osv.scenarios import Profile, Suite
from osv.worker import Worker


def certificates(directory):
    """Create an independent CA and localhost-only server certificate; expect hostname checks to reject the IP endpoint."""
    directory.chmod(0o700)
    def run(*args):
        subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    ca, key = directory / 'ca.crt', directory / 'ca.key'
    run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '3',
        '-subj', '/CN=OSV local TLS CA ' + uuid.uuid4().hex, '-keyout', str(key), '-out', str(ca),
        '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    server = directory / 'server'
    server.mkdir()
    run('openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=localhost',
        '-keyout', str(server / 'private.key'), '-out', str(directory / 'server.csr'))
    extensions = directory / 'server.ext'
    extensions.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost\n')
    run('openssl', 'x509', '-req', '-in', str(directory / 'server.csr'), '-CA', str(ca), '-CAkey', str(key),
        '-CAcreateserial', '-days', '3', '-sha256', '-extfile', str(extensions), '-out', str(server / 'public.crt'))
    key.chmod(0o600)
    (server / 'private.key').chmod(0o600)
    truststore = directory / 'java-truststore.p12'
    run('keytool', '-importcert', '-noprompt', '-alias', 'osv-local-ca', '-file', str(ca),
        '-keystore', str(truststore), '-storetype', 'PKCS12', '-storepass', 'changeit')
    system_ca = Path(ssl.get_default_verify_paths().cafile).resolve()
    bundle = directory / 'cpp-ca-bundle.pem'
    bundle.write_bytes(system_ca.read_bytes() + b'\n' + ca.read_bytes())
    return ca, server, truststore, system_ca, bundle


def recheck_registry(report_path):
    """Select only real production cases previously blocked by HTTP, before any case IO occurs."""
    previous = json.loads(report_path.read_text())
    selected = frozenset(row['case'] for row in previous['cases'] if row['status'] == 'fail'
        and 'must be made over a secure connection' in row.get('error', ''))
    if not selected:
        raise ValueError('recheck report contains no HTTPS-blocked cases')
    original_build = cli.build_registry
    def build(config):
        registry = original_build(config)
        suites = []
        for name in registry.profiles['complete'].suites:
            original = registry.suites[name]
            rows = tuple(row for row in original.catalog if row['case'] in selected)
            if not rows:
                continue
            chosen = frozenset(row['case'] for row in rows)
            suite_name = 'tls_' + name.removeprefix('ceph_')
            registry.add_suite(Suite(suite_name, frozenset(op for row in rows for op in row['operations']),
                lambda ctx, original=original, chosen=chosen: original.execute(replace(ctx, selected_cases=chosen)),
                rows, case_capabilities=True))
            suites.append(suite_name)
        registry.add_profile(Profile('tls-recheck', tuple(suites), '重跑原先被 HTTP 阻断的真实 S3 SDK 用例；TLS 证书及主机名校验开启'))
        return registry
    cli.build_registry = build
    return selected


def tls_probe(target, trusted, endpoint, original_command):
    """Send actual SDK HEAD requests; expect success with trusted matching TLS and certificate errors otherwise."""
    candidate = {**target, 'env': {**target['env'], 'OSV_ENDPOINT': endpoint}}
    if not trusted:
        candidate['command'] = original_command
    worker = Worker(candidate, 30)
    try:
        result = worker.call({'op': 'bucket_head'})
        return {'ok': result.get('ok'), 'error': result.get('error', ''), 'status_code': result.get('status_code')}
    finally:
        worker.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True, help='existing Java/C++ S3 manifest')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--recheck-report', type=Path, help='select previous HTTPS-blocked cases; otherwise run complete')
    parser.add_argument('--image', default='osv/minio:RELEASE.2025-02-03T21-03-04Z')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(exist_ok=False, parents=True)
    for executable in ('docker', 'openssl', 'keytool', 'bwrap'):
        if not shutil.which(executable):
            raise RuntimeError('required local TLS fixture command missing: ' + executable)
    work = Path(tempfile.mkdtemp(prefix='osv-minio-tls-'))
    ca, server, truststore, system_ca, bundle = certificates(work)
    for name in list(os.environ):
        if name.startswith(('OSV_', 'AWS_')):
            del os.environ[name]
    os.environ.update(OSV_ACCESS_KEY='osvtls', OSV_SECRET_KEY='osv-tls-local-secret',
        AWS_EC2_METADATA_DISABLED='true', REQUESTS_CA_BUNDLE=str(ca))
    selected = recheck_registry(args.recheck_report) if args.recheck_report else None
    container = 'osv-minio-tls-' + uuid.uuid4().hex[:8]
    subprocess.run(['docker', 'run', '-d', '--name', container, '--cpus=2', '--memory=2g', '--memory-swap=2g',
        '--blkio-weight=100', '-p', '127.0.0.1::9000', '-v', str(server) + ':/certs:ro',
        '-e', 'MINIO_ROOT_USER=osvtls', '-e', 'MINIO_ROOT_PASSWORD=osv-tls-local-secret',
        args.image, '--certs-dir', '/certs', 'server', '/data'], check=True, stdout=subprocess.DEVNULL)
    deployment = {'image': args.image, 'container': container, 'certificate_directory': str(work)}
    try:
        port = subprocess.check_output(['docker', 'port', container, '9000/tcp'], text=True).strip().split(':')[-1]
        endpoint = 'https://localhost:' + port
        deployment.update(endpoint=endpoint, version=subprocess.check_output(['docker', 'exec', container, 'minio', '--version'], text=True).strip())
        context = ssl.create_default_context(cafile=str(ca))
        for attempt in range(60):
            try:
                urllib.request.urlopen(endpoint + '/minio/health/live', context=context, timeout=2).close()
                break
            except Exception:
                if attempt == 59:
                    raise
                time.sleep(.5)
        manifest = json.loads(args.manifest.read_text())
        manifest['workload'].pop('compatibility_suites', None)
        manifest['workload'].update(mode='functional', profile='tls-recheck' if selected else 'complete',
            ceph_http={'endpoint': endpoint, 'addressing_style': 'path'})
        probes = []
        for target in manifest['targets']:
            target['env'].update(OSV_ENDPOINT=endpoint, OSV_BUCKET='osv-tls-' + target['language'] + '-' + uuid.uuid4().hex[:12])
            original = list(target['command'])
            if target['language'] == 'java':
                target['command'][1:1] = ['-Djavax.net.ssl.trustStore=' + str(truststore),
                    '-Djavax.net.ssl.trustStorePassword=changeit', '-Djavax.net.ssl.trustStoreType=PKCS12']
            elif target['language'] == 'cpp':
                # Mount the trusted bundle only in this SDK process namespace;
                # preserve the host trust store and the SDK's default verification.
                target['command'] = ['bwrap', '--ro-bind', '/', '/', '--bind', '/tmp', '/tmp',
                    '--dev-bind', '/dev', '/dev', '--proc', '/proc', '--ro-bind', str(bundle), str(system_ca), '--', *original]
            else:
                raise ValueError('TLS fixture supports Java/C++ S3 targets only')
            worker = Worker(target, 30)
            try:
                created = worker.call({'op': 'bucket_create'})
                if not created.get('ok'):
                    raise RuntimeError('trusted TLS bucket creation failed: ' + str(created))
            finally:
                worker.close()
            trusted = tls_probe(target, True, endpoint, original)
            untrusted = tls_probe(target, False, endpoint, original)
            mismatch = tls_probe(target, True, 'https://127.0.0.1:' + port, original)
            assert trusted['ok'], trusted
            for rejected in (untrusted, mismatch):
                assert rejected['ok'] is False and re.search(r'certificate|PKIX|hostname|SSL peer|subject alternative', rejected['error'], re.I), rejected
            probes.append({'target': target['id'], 'trusted_matching_endpoint': trusted,
                'untrusted_certificate': untrusted, 'hostname_mismatch': mismatch})
            print('TLS_VERIFIED', target['id'], flush=True)
        path = output / 'manifest.json'
        path.write_text(json.dumps(manifest, indent=2) + '\n')
        (output / 'tls-verification.json').write_text(json.dumps(probes, indent=2) + '\n')
        shutil.copy2(ca, output / 'ca.crt')
        config = cli.load_config(path)
        report = cli.run(config, output / 'report')
        rows = [row for row in report['cases'] if row.get('suite')]
        if selected:
            for target in manifest['targets']:
                assert {row['case'] for row in rows if row['target'] == target['id']} == selected
        summary = {'endpoint': endpoint, 'cases_per_sdk': len(report['coverage']['catalog']),
            'targets': {target['id']: dict(Counter(row['status'] for row in rows if row['target'] == target['id'])) for target in manifest['targets']},
            'remaining_https_rejections': [row for row in rows if 'must be made over a secure connection' in row.get('error', '')],
            'failures': [row for row in rows if row['status'] == 'fail'], 'execution_errors': report['execution_errors']}
        (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        print('SUMMARY', json.dumps({k: v for k, v in summary.items() if k != 'failures'}, ensure_ascii=False), flush=True)
    finally:
        (output / 'minio.log').write_bytes(subprocess.check_output(['docker', 'logs', container], stderr=subprocess.STDOUT))
        subprocess.run(['docker', 'stop', '-t', '5', container], stdout=subprocess.DEVNULL, check=False)
        deployment['status'] = 'stopped after test'
        (output / 'deployment.json').write_text(json.dumps(deployment, indent=2) + '\n')


if __name__ == '__main__':
    main()
