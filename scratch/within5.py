import csv
t={r['request_id']:r for r in csv.DictReader(open('dataset/sample_requests.csv'))}
m={r['request_id']:r for r in csv.DictReader(open('code/evaluation/sample_output.csv'))}
n=0
for k in t:
    a,b=float(m[k]['amount_safe_to_pay']),float(t[k]['amount_safe_to_pay'])
    ok=abs(a-b)<=0.05*abs(b) if b else a==b
    n+=ok
    print(k, a, b, 'OK' if ok else f'{(a-b)/b*100:+.1f}%' if b else 'X')
print('within5', n)
