"""Bounded loss-weight candidates selected on earlier chronological folds.

No test or future outcomes enter this module. Arrays are M x O x Q x H.
"""
from __future__ import annotations
import numpy as np


def pinball(y,p,q):
    e=y-p
    return np.maximum(q*e,(q-1)*e)


def weights(pred,y,quantiles,method):
    # M x O x Q x H -> Q x M. Candidate uses completed calibration origins only.
    m,_,nq,_=pred.shape
    support=np.isfinite(pred).all(axis=(1,3)).T
    prior=support.astype(float);prior/=prior.sum(axis=1,keepdims=True)
    if method=='equal':return prior
    family,parameter,shrink=method.split(':');parameter=float(parameter);shrink=float(shrink)
    loss=pinball(y[None,:,None,:],pred,np.asarray(quantiles)[None,None,:,None])
    if family.endswith('_shared'):
        common=np.flatnonzero(support.all(axis=1))
        raw=np.mean(loss[:,:,common,:],axis=(1,2,3))
        scores=np.tile(raw,(nq,1))
    else:
        # Missing native tails are structural, not observations with zero loss.
        scores=np.full((nq,m),np.nan)
        for qi in range(nq):
            for mi in np.flatnonzero(support[qi]):
                scores[qi,mi]=np.mean(loss[mi,:,qi,:])
    w=np.zeros((nq,m))
    for qi in range(nq):
        active=np.flatnonzero(support[qi]);s=scores[qi,active]
        floor=max(float(np.median(s))*1e-5,1e-10)
        if family.startswith('inverse'):
            z=np.maximum(s,floor)**(-parameter);z/=z.sum()
        elif family.startswith('softmax'):
            z=-parameter*s/max(float(np.mean(s)),floor);z-=np.max(z);z=np.exp(z);z/=z.sum()
        elif family=='stack_shared':
            raise ValueError('shared stack has a separate solver')
        else:raise ValueError(method)
        w[qi,active]=(1-shrink)*z+shrink/len(active)
    return w


def pool(pred,w):
    return np.sort(np.einsum('moqh,qm->oqh',np.nan_to_num(pred),w),axis=1)


CANDIDATES=['equal']+[
 f'{family}:{p}:{shrink}' for family in ['inverse_shared','inverse_quantile','softmax_shared','softmax_quantile']
 for p in [1,2,4] for shrink in [0,.2]
]


def choose_on_calibration(pred,y,q):
    # Expanding inner validation entirely within calibration origins.
    n=y.shape[0]
    splits=[(2,3),(3,4)] if n==4 else [(n//2,3*n//4),(3*n//4,n)]
    scores={}
    for method in CANDIDATES:
        folds=[]
        for split,end in splits:
            fitted=weights(pred[:,:split],y[:split],q,method)
            p=pool(pred[:,split:end],fitted)
            folds.append(float(np.mean(pinball(y[split:end,None,:],p,np.array(q)[None,:,None]))))
        scores[method]=float(np.mean(folds))
    # Equal is first for deterministic tie breaking. This selector cannot see test labels.
    selected=min(CANDIDATES,key=lambda k:scores[k])
    return selected,scores
