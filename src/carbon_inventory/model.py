from datetime import date
from fractions import Fraction
from .common import require,ident,number,rows
from .inventory import calculate

def day(v):
    if not isinstance(v,str):raise ValueError('expected ISO date')
    try:return date.fromisoformat(v)
    except ValueError:raise ValueError('invalid ISO date')

def analyze(data):
    require(data,'activities factors');factors={}
    for f in rows(data['factors'],1000):
        require(f,'id category unit kgco2e_per_unit valid_from valid_to')
        fid=ident(f['id']);category=ident(f['category']);unit=ident(f['unit']);rate=ident(f['kgco2e_per_unit'])
        if fid in factors or '/' not in rate:raise ValueError('duplicate factor or expected rational rate')
        n,d=rate.split('/')
        rate=Fraction(number(int(n),0,10**12),number(int(d),1,10**12))
        start=day(f['valid_from']);end=None if f['valid_to'] is None else day(f['valid_to'])
        if end is not None and end<=start:raise ValueError('factor interval must be nonempty')
        factors[fid]={'category':category,'unit':unit,'rate':rate,'start':start,'end':end}
    activities=[]
    for a in rows(data['activities'],20000):
        require(a,'id organization date scope category quantity unit factor_id')
        aid=ident(a['id']);org=ident(a['organization']);d=day(a['date']);scope=number(a['scope'],1,2);category=ident(a['category']);quantity=number(a['quantity'],0,10**12);unit=ident(a['unit']);fid=ident(a['factor_id'])
        if fid not in factors:raise ValueError('unknown emission factor')
        f=factors[fid]
        if f['category']!=category or f['unit']!=unit or d<f['start'] or (f['end'] and d>=f['end']):raise ValueError('factor does not match activity')
        activities.append({'id':aid,'organization':org,'date':d.isoformat(),'scope':scope,'category':category,'quantity':quantity,'unit':unit,'factor_id':fid,'rate':f['rate']})
    return calculate(activities)
