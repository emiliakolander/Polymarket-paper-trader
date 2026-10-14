# Persistence check (mock): PASSED

| Check | Result | Detail |
|---|---|---|
| same data source | pass | MOCK -> MOCK |
| virtual capital is $1,000 and deposited once | pass | capital 1000.0, deposits 1 |
| earlier runs kept, exactly one new run | pass | 1 -> 2 runs |
| earlier ledger rows unchanged | pass | 3 rows hashed |
| cash reconciles with ledger | pass | 990.0060 + 0.0000 = 990.0060 |
| all earlier orders present and unchanged | pass | 2 earlier orders; missing 0, changed 0 |
| earlier positions present; closures have a recorded reason | pass | 2 earlier positions; problems: [] |
| one more equity snapshot | pass | 1 -> 2 |
| decision history only appended | pass | signals 1->2, forecasts 0->0 |
