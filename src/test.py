import pandas as pd
p = pd.read_parquet('data/warehouse/predispatchprice/2026.parquet')
a = p[p.source == 'report_archive']
print(len(a), 'archive rows')
print('share stamped at exact midnight:', (a.known_at.dt.strftime('%H:%M:%S') == '00:00:00').mean())