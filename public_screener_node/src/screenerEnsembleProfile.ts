export const SCREENER_MODELS=["prophet","toto","granite","chronos","timesfm","timestral","t0"];
export const SCREENER_WEIGHT_POLICY="rolling_loss_selection_v2";
export function optimizedScreenerProfile(config:any,runs:any,weighting:any):boolean {
  return config?.weight_mode==="auto" && config?.weight_policy===SCREENER_WEIGHT_POLICY && config?.toto_variant==="4m" &&
    Object.keys(config.models||{}).length===7 && SCREENER_MODELS.every(id=>config.models[id]?.enabled===true && config.models[id].weight>0) &&
    Array.isArray(runs) && runs.length===7 && SCREENER_MODELS.every(id=>runs.some(m=>m.id===id && m.status==="completed")) &&
    weighting?.mode==="auto" && weighting.policy===SCREENER_WEIGHT_POLICY && ["optimized","fallback"].includes(weighting.status);
}
