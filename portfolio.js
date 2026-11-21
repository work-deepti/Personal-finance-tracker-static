'use strict';
const YR = 31557600000;
const dt = h => h.type === 'fd' ? h.start : h.date;
function fdValue(h){const n=+h.freq||4,t=Math.max(0,(Math.min(Date.now(),new Date(h.maturity))-new Date(h.start))/YR);return h.principal*Math.pow(1+h.rate/100/n,n*t)}
function calc(h, unitPrice){let inv,cur=null;
 if(h.type==='fd'){inv=+h.principal;cur=fdValue(h)}
 else if(h.type==='manual'){inv=+(h.principal??h.value);cur=+(h.value??h.principal)}
 else{inv=h.currency&&h.currency!=='INR'&&!h.fx_buy?null:h.units*h.buy*(+h.fx_buy||1);cur=unitPrice==null?null:h.units*unitPrice}
 return h.closed_date?{inv,out:+h.closed_value,closed:true}:{inv,cur}}
function ann(h,inv,val){const d=dt(h);if(!d)return'';
 const end=h.closed_date?new Date(h.closed_date):h.type==='fd'?Math.min(Date.now(),new Date(h.maturity)):Date.now(),y=(end-new Date(d))/YR;
 return y>=1&&inv>0&&val>0?`<small>${((Math.pow(val/inv,1/y)-1)*100).toFixed(1)}% a year</small>`:''}
function xirr(f){const t0=f[0][0],npv=r=>f.reduce((s,[d,a])=>s+a/Math.pow(1+r,(d-t0)/YR),0);
 let lo=-0.99,hi=10;if(npv(lo)*npv(hi)>0)return null;
 for(let i=0;i<100;i++){const m=(lo+hi)/2;npv(lo)*npv(m)<=0?hi=m:lo=m}return(lo+hi)/2}
