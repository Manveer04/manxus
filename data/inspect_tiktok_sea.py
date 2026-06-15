import json
data = json.load(open('/mnt/GombakWale/inventory-sync/data/tiktok_product_apis.json'))
for r in data:
    if 'sea_product' in r['url']:
        print('URL: ' + r['url'][:80])
        print(json.dumps(r['data'], indent=2, default=str)[:2000])