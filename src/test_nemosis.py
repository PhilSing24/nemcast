from nemosis import dynamic_data_compiler

df = dynamic_data_compiler(
    '2023/01/01 00:00:00', '2023/02/01 00:00:00',
    'DISPATCHPRICE', 'data/raw',
    filter_cols=['REGIONID'], filter_values=(['SA1'],)
)
print(df.shape)
print(df.head())