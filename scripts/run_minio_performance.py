#!/usr/bin/env python3
"""Run the real performance harness against loopback MinIO without Docker port forwarding."""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from osv.cli import load_config, run
from osv.worker import Worker


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,default=Path('examples/minio-sdk-performance.json'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--image',default='osv/minio:RELEASE.2025-02-03T21-03-04Z')
    args=parser.parse_args()
    if not sys.platform.startswith('linux'):
        raise RuntimeError('This local fixture requires Linux host networking')
    args.output.mkdir(parents=True,exist_ok=False)
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
    endpoint=f'http://127.0.0.1:{port}'
    name='osv-performance-'+uuid.uuid4().hex[:12]
    password=secrets.token_urlsafe(24)
    environment={**os.environ,'MINIO_ROOT_USER':'osvlocal','MINIO_ROOT_PASSWORD':password}
    # Bind only to loopback. Docker userland forwarding exhausts its outbound ports
    # under SDKs which create a new connection per request.
    subprocess.run(['docker','run','-d','--name',name,'--network','host','--cpus=4','--memory=4g','--memory-swap=4g',
        '-e','MINIO_ROOT_USER','-e','MINIO_ROOT_PASSWORD','-e','MINIO_BROWSER=off',args.image,
        'server','--address',f'127.0.0.1:{port}','/data'],env=environment,check=True,stdout=subprocess.DEVNULL)
    previous={key:os.environ.get(key) for key in ('OSV_ACCESS_KEY','OSV_SECRET_KEY')}
    os.environ.update(OSV_ACCESS_KEY='osvlocal',OSV_SECRET_KEY=password)
    try:
        for attempt in range(60):
            try:
                urllib.request.urlopen(endpoint+'/minio/health/live',timeout=2).close();break
            except OSError:
                if attempt==59:raise
                time.sleep(.5)
        config=json.loads(args.manifest.read_text())
        config.setdefault('workload',{})['mode']='performance'
        for target in config['targets']:
            target.setdefault('env',{}).update(OSV_ENDPOINT=endpoint,OSV_REGION='us-east-1',OSV_BUCKET='osv-performance',
                OSV_PATH_STYLE='true')
        created=False
        for target in config['targets']:
            worker=Worker(target,60)
            try:
                if 'bucket_create' in worker.capabilities:
                    result=worker.call({'op':'bucket_create'})
                    if not result['ok']:raise RuntimeError(result.get('error','bucket creation failed'))
                    created=True;break
            finally:worker.close()
        if not created:raise ValueError('manifest needs one driver supporting bucket_create for local setup')
        with tempfile.TemporaryDirectory(prefix='osv-manifest-') as temporary:
            manifest=Path(temporary)/'manifest.json';manifest.write_text(json.dumps(config));manifest.chmod(0o600)
            report=run(load_config(manifest),args.output/'results')
        print(args.output/'results/report.md')
        if report['execution_errors'] or any(not s['ok'] for s in report['samples']):return 1
        return 0
    finally:
        for key,value in previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value
        logs=subprocess.run(['docker','logs',name],capture_output=True,text=True)
        (args.output/'minio.log').write_text(logs.stdout+logs.stderr)
        (args.output/'deployment.json').write_text(json.dumps({'image':args.image,'endpoint':endpoint,'network':'host-loopback','cpu_limit':4,'memory_limit':'4g'}))
        subprocess.run(['docker','rm','-fv',name],check=True,stdout=subprocess.DEVNULL)


if __name__=='__main__':sys.exit(main())
