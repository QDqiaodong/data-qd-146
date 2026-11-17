DESCRIPTION='Traceable scope 1 and 2 greenhouse gas inventory with dated emission factors'
import argparse, json, sqlite3, sys
from pathlib import Path
from .audit import run as run_audit
from .common import read, save, save_bytes, render
from .group import run_group
from .model import analyze
from .registry import Store, adjudicate
from .rounding import run as run_rounding

def run_source(source):
    data,digest=read(source)
    return {'schema_version':1,'source_sha256':digest,'result':analyze(data)}

def run_uncertainty(source,spec):
    data,digest=read(source)
    spec_data,spec_digest=read(spec)
    return run_audit(data,digest,spec_data,spec_digest)

def run_rounding_mode(source,spec):
    data,digest=read(source)
    spec_data,spec_digest=read(spec)
    return run_rounding(data,digest,spec_data,spec_digest)

def run_registry(registry_path,batch_path):
    if registry_path.is_symlink():
        raise ValueError('registry database cannot be a symlink')
    batch,_=read(batch_path)
    with Store(registry_path) as store:
        mode,report,generation=adjudicate(store,batch)
    return mode,report,generation

def main():
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--source',type=Path,help='full-inventory JSON input')
    mode.add_argument('--registry',type=Path,help='SQLite ledger database path')
    parser.add_argument('--batch',type=Path,help='revision batch JSON (registry mode)')
    parser.add_argument('--boundary',type=Path,help='dated ownership boundary JSON (group mode)')
    parser.add_argument('--root',help='consolidating organization (group mode)')
    parser.add_argument('--uncertainty',type=Path,help='shared-risk audit spec (audit mode)')
    parser.add_argument('--rounding',type=Path,help='controlled rounding disclosure spec (rounding mode)')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        if args.rounding is not None:
            if args.registry is not None:
                raise ValueError('--rounding cannot be combined with --registry')
            if args.batch is not None:
                raise ValueError('--rounding cannot be combined with --batch')
            if args.boundary is not None or args.root is not None:
                raise ValueError('--rounding cannot be combined with --boundary/--root')
            if args.uncertainty is not None:
                raise ValueError('--rounding cannot be combined with --uncertainty')
            if args.source is None:
                raise ValueError('--rounding requires --source')
            if args.source.resolve()==args.rounding.resolve() or args.source.resolve()==args.output.resolve() or args.rounding.resolve()==args.output.resolve():
                raise ValueError('source, rounding and output paths must differ')
            report=run_rounding_mode(args.source,args.rounding)
            save(args.output,report)
            print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0
        if args.uncertainty is not None:
            if args.registry is not None:
                raise ValueError('--uncertainty cannot be combined with --registry')
            if args.batch is not None:
                raise ValueError('--uncertainty cannot be combined with --batch')
            if args.boundary is not None or args.root is not None:
                raise ValueError('--uncertainty cannot be combined with '
                                 '--boundary/--root')
            if args.source is None:
                raise ValueError('--uncertainty requires --source')
            if args.source.resolve()==args.uncertainty.resolve() or args.source.resolve()==args.output.resolve() or args.uncertainty.resolve()==args.output.resolve():
                raise ValueError('source, uncertainty and output paths must differ')
            report=run_uncertainty(args.source,args.uncertainty)
            save(args.output,report)
            print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0
        if args.boundary is not None or args.root is not None:
            if args.source is None:
                raise ValueError('--boundary/--root require --source')
            if args.boundary is None or args.root is None:
                raise ValueError('--boundary and --root must be supplied together')
            if args.batch is not None:
                raise ValueError('--batch requires --registry')
            if args.source.resolve()==args.output.resolve() or args.boundary.resolve()==args.output.resolve() or args.source.resolve()==args.boundary.resolve():
                raise ValueError('source, boundary and output paths must differ')
            report=run_group(args.source,args.boundary,args.root)
            save(args.output,report)
            print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0
        if args.source is not None:
            if args.batch is not None:
                raise ValueError('--batch requires --registry')
            if args.source.resolve()==args.output.resolve():
                raise ValueError('input and output must differ')
            report=run_source(args.source)
            save(args.output,report)
            print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0
        if args.batch is None:
            raise ValueError('--batch is required with --registry')
        if args.registry.resolve()==args.output.resolve() or args.batch.resolve()==args.output.resolve() or args.registry.resolve()==args.batch.resolve():
            raise ValueError('registry, batch and output paths must differ')
        mode,report,generation=run_registry(args.registry,args.batch)
        try:
            save_bytes(args.output,render(report))
        except OSError as error:
            raise ValueError('generation %d was accepted but report file write failed (%s); replay the same batch to repair the report'%(generation,error))
        print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError,sqlite3.Error) as error:
        print(str(error),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
