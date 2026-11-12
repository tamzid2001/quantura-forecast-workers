"""Real CPU inference through the unchanged frozen sports Toto adapter."""
from datetime import datetime,timedelta,timezone
import importlib.metadata
import json
from pathlib import Path
import numpy as np
import torch
from ensemble_forecasting.adapters.toto import TotoAdapter
from ensemble_forecasting.schemas import ForecastRequest,PreparedSeries
from market_research.game_p01_month import runtime_fingerprint

output=Path('artifacts/frozen-toto-smoke.json');output.parent.mkdir(exist_ok=True)
report={'schema':'quantura-frozen-toto-smoke-v1','provenance':'FIXTURE','hardware':'CPU',
        'status':'failed','runtime_fingerprint':runtime_fingerprint(),'real_market_forecasts':0,
        'packages':{n:importlib.metadata.version(n) for n in ['toto-2','torch','huggingface-hub']},'cases':[]}
try:
    torch.set_num_threads(2);torch.manual_seed(20261112)
    request=ForecastRequest.from_dict({'prediction_length':7,'horizon_mode':'frequency_periods',
        'quantiles':[.01,.25,.5,.75,.9,.99],'transform':'none','context_length':500,
        'models':{'toto':{'enabled':True,'weight':1}},'frequency':'1h','calendar':'NONE','runtime_mode':'test'})
    now=datetime(2026,11,12,tzinfo=timezone.utc)
    for size in [32,44,500]:
        # These are explicitly synthetic values, not invented market history.
        values=np.linspace(1.,2.,size)
        times=tuple((now-timedelta(hours=size-i)).isoformat() for i in range(size))
        series=PreparedSeries(times,values,values,'none','1h','UTC','synthetic-loading-diagnostic')
        result=TotoAdapter().forecast(series,tuple((now+timedelta(hours=i)).isoformat() for i in range(7)),request)
        result.validate()
        assert result.checkpoint_revision=='8306a9801cf98c0f5ffe4b2dcc8f496e616d84d9'
        assert .01 not in result.available_quantiles and .99 not in result.available_quantiles
        report['cases'].append({'context_rows':size,'shape':list(result.quantile_matrix.shape),
                                'available_quantiles':list(result.available_quantiles),'duration_seconds':result.duration_seconds})
    report['status']='passed'
except Exception as error:
    # Do not serialize request contents, provider errors or credentials.
    chain=[];seen=set()
    while error is not None and id(error) not in seen:
        seen.add(id(error));chain.append(type(error).__name__);error=error.__cause__ or error.__context__
    report['exception_types']=chain
finally:
    output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    print(json.dumps(report),flush=True)
raise SystemExit(0 if report['status']=='passed' else 1)
