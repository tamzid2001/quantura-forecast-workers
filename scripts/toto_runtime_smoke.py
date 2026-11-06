"""CPU-only SDK diagnostic using declared synthetic fixtures, never forecasts.

No market data, account data, trade, Metaculus write or billing API is used.
Failure diagnostics contain stage, exception types and code locations only.
"""
from __future__ import annotations

from datetime import datetime,timedelta,timezone
import importlib.metadata
import json
import os
from pathlib import Path
import traceback


def main():
    output=Path('artifacts/toto-runtime-smoke.json');output.parent.mkdir(exist_ok=True)
    report={'provenance':'FIXTURE','execution':'real_cpu_model_sdk','hardware':'CPU',
            'status':'failed','stage':'import','market_forecasts_published':0,'live_orders':0}
    try:
        import numpy as np
        import torch
        from ensemble_forecasting.adapters.toto import TotoAdapter,toto_checkpoint
        from ensemble_forecasting.adapters.toto_loader import load_toto_model
        from ensemble_forecasting.schemas import ForecastRequest,PreparedSeries
        torch.set_num_threads(2)
        torch.manual_seed(20261105)
        report['packages']={p:importlib.metadata.version(p) for p in ['toto-2','torch','huggingface-hub']}
        request=ForecastRequest.from_dict({'prediction_length':7,'horizon_mode':'frequency_periods',
            'quantiles':[.01,.25,.5,.75,.9,.99],'transform':'none','context_length':512,
            'models':{'toto':{'enabled':True,'weight':1}},'frequency':'1D','calendar':'NONE'})
        checkpoint,revision=toto_checkpoint(request)
        report.update(checkpoint=checkpoint,revision=revision,stage='native_checkpoint_load')
        model=load_toto_model(checkpoint,revision,token=os.environ.get('HF_TOKEN')).to('cpu').eval()
        target=torch.linspace(1.,2.,64).reshape(1,1,-1)
        with torch.inference_mode():
            report['stage']='native_forecast'
            native=model.forecast({'target':target,'target_mask':torch.ones_like(target,dtype=torch.bool),
                                   'series_ids':torch.zeros((1,1),dtype=torch.long)},
                                  horizon=7,decode_block_size=None,has_missing_values=False)
        assert tuple(native.shape)==(9,1,1,7) and torch.isfinite(native).all()
        report['native_shape']=list(native.shape)
        del model
        report['adapter_cases']=[]
        for size in [512,511]:
            report['stage']='adapter_context_'+str(size)
            values=np.linspace(1.,2.,size)
            now=datetime(2026,11,5,tzinfo=timezone.utc)
            times=tuple((now-timedelta(days=size-i)).isoformat() for i in range(size))
            series=PreparedSeries(times,values,values,'none','1D','UTC','fixture-not-market-data')
            result=TotoAdapter().forecast(series,tuple((now+timedelta(days=i)).isoformat() for i in range(7)),request)
            result.validate()
            assert .01 not in result.available_quantiles and .99 not in result.available_quantiles
            report['adapter_cases'].append({'genuine_fixture_context_rows':size,
                                           'available_quantiles':list(result.available_quantiles),
                                           'shape':list(result.quantile_matrix.shape)})
        report.update(status='passed',stage='complete')
    except Exception as error:
        chain=[];seen=set();current=error
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            chain.append({'type':type(current).__name__,
                          'locations':[f'{Path(f.filename).name}:{f.lineno}:{f.name}' for f in traceback.extract_tb(current.__traceback__)],
                          'http_status':getattr(getattr(current,'response',None),'status_code',None)})
            current=current.__cause__ or current.__context__
        report['exception_chain']=chain
    output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    print(json.dumps(report,sort_keys=True))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
