from collections import defaultdict
from fractions import Fraction

def calculate(activities):
    groups=defaultdict(lambda:Fraction());details=[]
    for a in sorted(activities,key=lambda x:x['id']):
        value=a['quantity']*a['rate'];key=(a['organization'],a['scope']);groups[key]+=value
        details.append({'id':a['id'],'organization':a['organization'],'date':a['date'],'scope':a['scope'],'category':a['category'],'quantity':a['quantity'],'unit':a['unit'],'factor_id':a['factor_id'],'kgco2e':str(value)})
    return {'activities':details,'totals':[{'organization':o,'scope':s,'kgco2e':str(v)} for (o,s),v in sorted(groups.items())]}
