import pandas as pd
from scanner import heikin_ashi, supertrend

df = pd.DataFrame({
    'open':[100,102,101,104,105,108],
    'high':[103,104,105,107,110,112],
    'low':[99,100,100,102,104,106],
    'close':[102,101,104,105,109,111],
}, index=pd.date_range('2026-01-05', periods=6, freq='W-MON', tz='Asia/Kolkata'))
ha = heikin_ashi(df)
st = supertrend(df, 1, 1)
assert len(ha) == 6
assert len(st) == 6
assert st.notna().sum() >= 5
print('indicator smoke test: PASS')
