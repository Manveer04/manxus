import json

data = json.load(open('/mnt/YourPool/manxus/data/lazada_products_raw.json'))
products = data.get('data', {}).get('productDTOList', [])
print('Total products in response: ' + str(len(products)))
print()
if products:
    p = products[0]
    print('=== First product keys ===')
    for k, v in p.items():
        print('  ' + str(k) + ': ' + str(v)[:80])