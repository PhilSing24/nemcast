from nemosis import dynamic_data_compiler

RAW = 'data/raw'
START, END = '2023/01/01 00:00:00', '2026/09/01 00:00:00'
REGIONS = ['NSW1', 'QLD1', 'VIC1', 'SA1', 'TAS1']

price = dynamic_data_compiler(
    START, END, 'DISPATCHPRICE', RAW,
    filter_cols=['REGIONID', 'INTERVENTION'],
    filter_values=(REGIONS, [0]),
)
print('price:', price.shape)
price.to_parquet('data/interim/dispatchprice.parquet', index=False)

regionsum = dynamic_data_compiler(
    START, END, 'DISPATCHREGIONSUM', RAW,
    filter_cols=['REGIONID', 'INTERVENTION'],
    filter_values=(REGIONS, [0]),
)
print('regionsum:', regionsum.shape)
regionsum.to_parquet('data/interim/dispatchregionsum.parquet', index=False)