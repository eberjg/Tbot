import coinbase_futures as cf
print("ALG:", cf.JWT_ALG)
ok = cf.auth_smoke_test()
print("Auth OK?", ok)
if ok:
    print("BTC perp price:", cf.get_futures_price("BTC-USD"))
    print("ETH perp price:", cf.get_futures_price("ETH-USD"))