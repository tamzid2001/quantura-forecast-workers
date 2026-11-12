"""Prepare an exact pinned local checkpoint for the unchanged Toto 2.0.0 SDK.

The SDK has a supported local-directory loader that builds Toto2ModelConfig.
Its remote Hub mixin fails before inference in the frozen sports runtime.
Materializing the registered model ID as a local path uses that loader without
editing any forecasting code, weights, observations, rules or result ledgers.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re

MODEL = 'Datadog/Toto-2.0-4m'
MAX_WEIGHT_BYTES = 128*1024*1024


def prepare(root, checkpoint, revision, downloader):
    root=Path(root).resolve()
    if checkpoint != MODEL or not isinstance(revision,str) or not re.fullmatch('[a-f0-9]{40}',revision):
        raise ValueError('UNAPPROVED_FROZEN_TOTO_CHECKPOINT')
    destination=root/checkpoint
    if destination.parent.is_symlink():
        raise ValueError('UNSAFE_MODEL_PARENT')
    if destination.exists() or destination.is_symlink():
        raise ValueError('REFUSING_MODEL_PATH_OVERWRITE')
    snapshot=Path(downloader(repo_id=checkpoint,revision=revision,token=False,
        allow_patterns=['config.json','*.safetensors','*.safetensors.index.json'])).resolve()
    if snapshot.name != revision or not snapshot.is_dir():
        raise ValueError('TOTO_SNAPSHOT_REVISION_MISMATCH')
    config=snapshot/'config.json'
    if not config.is_file() or config.stat().st_size>1024*1024:
        raise ValueError('INVALID_TOTO_CONFIGURATION')
    json.loads(config.read_text())
    index=snapshot/'model.safetensors.index.json'
    if index.exists():
        if index.stat().st_size>1024*1024:raise ValueError('INVALID_TOTO_WEIGHT_INDEX')
        mapping=json.loads(index.read_text()).get('weight_map')
        if not isinstance(mapping,dict) or not mapping:raise ValueError('INVALID_TOTO_WEIGHT_INDEX')
        names=set(mapping.values())
    else:names={'model.safetensors'}
    if any(not isinstance(n,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.safetensors',n) for n in names):
        raise ValueError('UNSAFE_TOTO_WEIGHT_PATH')
    files=[config]+([index] if index.exists() else [])+[snapshot/n for n in sorted(names)]
    total=0;checksums={}
    for file in files:
        if not file.is_file():raise ValueError('INCOMPLETE_TOTO_SNAPSHOT')
        total+=file.stat().st_size
        if total>MAX_WEIGHT_BYTES:raise ValueError('TOTO_SNAPSHOT_TOO_LARGE')
        checksums[file.name]=hashlib.sha256(file.read_bytes()).hexdigest()
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.symlink_to(snapshot,target_is_directory=True)
    return {'schema':'quantura-frozen-toto-loading-v1','checkpoint':checkpoint,'revision':revision,
            'loader':'toto-2-2.0.0-supported-local-directory','forecast_source_modified':False,
            'weights_modified':False,'files_sha256':checksums,'snapshot_bytes':total}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path.cwd())
    parser.add_argument('--receipt',type=Path,required=True)
    args=parser.parse_args()
    # Read the registry from the frozen checkout, never from a newer controller.
    registry=json.loads((args.root/'quantura_site/functions_explore/src/ensembleModelRegistry.json').read_text())
    model=registry['models']['toto']
    from huggingface_hub import snapshot_download
    receipt=prepare(args.root,model['checkpoint'],model['checkpointRevision'],snapshot_download)
    receipt['frozen_code_sha']=os.environ.get('CODE_SHA')
    args.receipt.parent.mkdir(parents=True,exist_ok=True)
    args.receipt.write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt),flush=True)


if __name__=='__main__':main()
